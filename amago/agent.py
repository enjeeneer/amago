# pylint: skip-file
# fmt: off
# test
from itertools import chain

import torch
from torch import nn
import torch.nn.functional as F
from einops import repeat, rearrange
import numpy as np
import wandb
import gin
import gymnasium as gym
from typing import Union

from agents.amago.amago.loading import Batch, MAGIC_PAD_VAL
from agents.amago.amago.nets.tstep_encoders import *
from agents.amago.amago.nets.traj_encoders import *
from agents.amago.amago.nets import actor_critic
from agents.amago.amago import utils
from agents.amago.amago.envs.env_utils import GPUSequenceBuffer
from agents.base import AmagoBatch


@gin.configurable
class Multigammas:
    def __init__(
        self,
        # fmt: off
        discrete = [.1, .9, .95, .97, .99, .995],
        continuous = [.1, .9, .95, .97, .99, .995],
        # fmt: on
    ):
        self.discrete = discrete
        self.continuous = continuous


@gin.configurable
class Agent(nn.Module):
    def __init__(
        self,
        obs_space: gym.spaces.Dict,
        goal_space: gym.spaces.Box,
        rl2_space: gym.spaces.Box,
        max_seq_len: int,
        horizon: int,
        device: torch.device,
        batch_size: int = 24,
        learning_rate: float = 1e-4,
        l2_coeff: float = 1e-3,
        critic_loss_weight: float = 10.0,
        lr_warmup_steps: int = 500,
        action_space: Optional[gym.spaces.Space] = None,
        action_dim: int = None,
        tstep_encoder_Cls=FFTstepEncoder,
        traj_encoder_Cls=TformerTrajEncoder,
        num_critics: int = 4,
        num_critics_td: int = 2,
        online_coeff: float = 1.0,
        offline_coeff: float = 0.1,
        gamma: float = 0.999,
        reward_multiplier: float = 10.0,
        tau: float = 0.003,
        fake_filter: bool = False,
        popart: bool = True,
        use_target_actor: bool = True,
        use_multigamma: bool = True,
    ):
        super().__init__()
        self.name = "Amago"
        self.obs_space = obs_space
        self.goal_space = goal_space
        self.rl2_space = rl2_space

        self.action_space = action_space
        self.multibinary = False
        self.discrete = False
        if action_dim is None:
            if isinstance(self.action_space, gym.spaces.Discrete):
                self.action_dim = self.action_space.n
                self.discrete = True
            elif isinstance(self.action_space, gym.spaces.MultiBinary):
                self.action_dim = self.action_space.n
                self.multibinary = True
            elif isinstance(action_space, gym.spaces.Box):
                self.action_dim = self.action_space.shape[-1]
            elif isinstance(action_space, gym.spaces.box.Box):
                self.action_dim = self.action_space.shape[-1]
            else:
                raise ValueError(f"Unsupported action space: `{type(self.action_space)}`")
        else:
            self.action_dim = action_dim

        self.reward_multiplier = reward_multiplier
        self.pad_val = MAGIC_PAD_VAL
        self.fake_filter = fake_filter
        self.offline_coeff = offline_coeff
        self.online_coeff = online_coeff
        self.tau = tau
        self.device = device
        self.use_target_actor = use_target_actor
        self.max_seq_len = max_seq_len
        self.batch_size = batch_size
        self._critic_loss_weight = critic_loss_weight

        self.tstep_encoder = tstep_encoder_Cls(
            obs_space=obs_space,
            goal_space=goal_space,
            rl2_space=rl2_space,
        )
        self.traj_encoder = traj_encoder_Cls(
            tstep_dim=self.tstep_encoder.emb_dim,
            max_seq_len=max_seq_len,
            horizon=horizon,
        )
        self.emb_dim = self.traj_encoder.emb_dim

        if self.discrete:
            multigammas = Multigammas().discrete
        else:
            multigammas = Multigammas().continuous

        # provided hparam `gamma` will stay in the -1 index
        # of gammas, actor, and critic outputs.
        gammas = (multigammas if use_multigamma else []) + [gamma]
        self.gammas = torch.Tensor(gammas).float()
        assert num_critics_td <= num_critics
        self.num_critics = num_critics
        self.num_critics_td = num_critics_td

        self.popart = actor_critic.PopArtLayer(gammas=len(gammas), enabled=popart)

        ac_kwargs = {
            "state_dim": self.traj_encoder.emb_dim,
            "action_dim": self.action_dim,
            "discrete": self.discrete,
            "gammas": self.gammas,
        }
        self.critics = actor_critic.NCritics(**ac_kwargs, num_critics=num_critics)
        self.target_critics = actor_critic.NCritics(
            **ac_kwargs, num_critics=num_critics
        )
        self.maximized_critics = actor_critic.NCritics(
            **ac_kwargs, num_critics=num_critics
        )
        if self.multibinary:
            ac_kwargs["cont_dist_kind"] = "multibinary"
        self.actor = actor_critic.Actor(**ac_kwargs)
        self.target_actor = actor_critic.Actor(**ac_kwargs)
        # full weight copy to targets
        self.hard_sync_targets()

        # optimizer
        self.optimizer = torch.optim.AdamW(
            self.trainable_params,
            lr=learning_rate,
            weight_decay=l2_coeff,
        )
        self.lr_schedule = utils.get_constant_schedule_with_warmup(
            optimizer=self.optimizer, num_warmup_steps=lr_warmup_steps
        )

    def update(self, batch: AmagoBatch, **kwargs) -> dict:
        """
        Core update step. Mimics the "train_step" fucntionality from
        the original repo:
        https://github.com/UT-Austin-RPL/amago/
        blob/5245bf436ab4e0159004ca5dfb7197edc0148bcd/amago/learning.py#L588-L603
        Args:
            batch: AmagoBatch of trajectories

        :return:
        """

        self.optimizer.zero_grad()
        loss_dict = self._compute_loss(batch)
        loss = loss_dict["actor_loss"] + self._critic_loss_weight * loss_dict["critic_loss"]
        loss.backward()
        self.optimizer.step()
        self.lr_schedule.step()

        return loss_dict

    def _compute_loss(self, batch: AmagoBatch):
        """
        Compute the Amago loss. Mimics the "compute_loss" function from
        the original repo:
        https://github.com/UT-Austin-RPL/amago/blob/
        5245bf436ab4e0159004ca5dfb7197edc0148bcd/amago/learning.py#L554-L574
        Args:
            batch: AmagoBatch of trajectories
        Returns:
            dict: loss dictionary
        """
        critic_loss, actor_loss = self.forward(batch, True)

        update_info = self.update_info
        B, L_1, G, _ = actor_loss.shape
        C = len(self.critics)
        state_mask = (~((batch.rl2s == MAGIC_PAD_VAL).all(-1, keepdim=True))).float()
        critic_state_mask = repeat(state_mask[:, 1:, ...], f"B L 1 -> B L {C} {G} 1")
        actor_state_mask = repeat(state_mask[:, :-1, ...], f"B L 1 -> B L {G} 1")

        masked_actor_loss = utils.masked_avg(actor_loss, actor_state_mask)
        if isinstance(critic_loss, torch.Tensor):
            masked_critic_loss = utils.masked_avg(critic_loss, critic_state_mask)
        else:
            assert critic_loss is None
            masked_critic_loss = 0.0

        return {
            "critic_loss": masked_critic_loss,
            "actor_loss": masked_actor_loss,
            "mask": state_mask,
        } | update_info


    def get_current_timestep(
        self, sequences: torch.Tensor | dict[torch.Tensor], seq_lengths: torch.Tensor
    ):
        dict_based = isinstance(sequences, dict)
        if not dict_based:
            sequences = {"dummy": sequences}
        timesteps = {}
        for k, v in sequences.items():
            missing_dims = v.ndim - seq_lengths.ndim
            seq_lengths_ = seq_lengths.reshape(seq_lengths.shape + (1,) * missing_dims)
            timesteps[k] = torch.take_along_dim(v, seq_lengths_ - 1, dim=1)
        if not dict_based:
            timesteps = timesteps["dummy"]
        return timesteps

    @property
    def trainable_params(self):
        """
        Returns iterable of all trainable parameters that should be passed to an optimzer. (Everything but the target networks).
        """
        return chain(
            self.tstep_encoder.parameters(),
            self.traj_encoder.parameters(),
            self.critics.parameters(),
            self.actor.parameters(),
        )

    def _full_copy(self, target, online):
        for target_param, param in zip(target.parameters(), online.parameters()):
            target_param.data.copy_(param.data)

    def _ema_copy(self, target, online):
        for target_param, param in zip(target.parameters(), online.parameters()):
            target_param.data.copy_(
                target_param.data * (1.0 - self.tau) + param.data * self.tau
            )

    def hard_sync_targets(self):
        """
        Hard copy online actor/critics to target actor/critics
        """
        self._full_copy(self.target_critics, self.critics)
        self._full_copy(self.target_actor, self.actor)
        self._full_copy(self.maximized_critics, self.critics)

    def soft_sync_targets(self):
        """
        EMA copy online actor/critics to target actor/critics (DDPG-style)
        """
        self._ema_copy(self.target_critics, self.critics)
        self._ema_copy(self.target_actor, self.actor)
        # full copy duplicate critic
        self._full_copy(self.maximized_critics, self.critics)

    def get_actions(
        self,
        obs,
        goals,
        rl2s,
        seq_lengths,
        time_idxs,
        hidden_state=None,
        sample: bool = True,
    ):
        """
        Get rollout actions from the current policy.
        """
        using_hidden = hidden_state is not None
        if using_hidden:
            obs = self.get_current_timestep(obs, seq_lengths)
            goals = self.get_current_timestep(goals, seq_lengths)
            rl2s = self.get_current_timestep(rl2s, seq_lengths)
            time_idxs = self.get_current_timestep(time_idxs, seq_lengths.squeeze(-1))
        tstep_emb = self.tstep_encoder(obs=obs, goals=goals, rl2s=rl2s)

        # sequence model embedding [batch, length, d_emb]
        traj_emb_t, hidden_state = self.traj_encoder(
            tstep_emb, time_idxs=time_idxs, hidden_state=hidden_state
        )
        if not using_hidden:
            traj_emb_t = self.get_current_timestep(traj_emb_t, seq_lengths)

        # generate action distribution [batch, len(self.gammas), d_action]
        action_dists = self.actor(traj_emb_t.squeeze(1))
        if sample:
            actions = action_dists.sample()
        else:
            if self.discrete:
                actions = torch.argmax(action_dists.probs, dim=-1, keepdim=True)
            else:
                actions = action_dists.mean

        # get intended gamma distribution (always in -1 idx)
        actions = actions[..., -1, :].cpu().float().numpy()
        if self.discrete:
            actions = actions.astype(np.uint8)
        else:
            actions = actions.astype(np.float32)
        return actions, hidden_state

    def forward(self, batch: Union[Batch, AmagoBatch], log_step: bool):
        """
        Main step of training loop. Generate actor and critic loss
        terms in a minimum number of forward passes with a compact
        sequence format. See comments for detailed explanation.
        """
        # fmt: off
        self.update_info = {}  # holds wandb stats
        active_log_dict = self.update_info if log_step else None

        ##########################
        ## Step 0: Timestep Emb ##
        ##########################
        o = self.tstep_encoder(obs=batch.obs, goals=batch.goals, rl2s=batch.rl2s, log_dict=active_log_dict)

        ###########################
        ## Step 1: Get Organized ##
        ###########################
        B, L, D_o = o.shape
        # padded actions are `self.pad_val` which will be invalid;
        # clip to valid range now and mask the loss later
        a = batch.actions
        a = a.clamp(0, 1.0) if self.discrete else a.clamp(-1., 1.)
        _B, _L, D_action = a.shape
        assert _L == L - 1
        G = len(self.gammas)
        # note that the last timestep does not have an action.
        # we give it a fake one to make shape math work.
        a_buffer = torch.cat((a, a[:, -1, ...].clone().unsqueeze(1)), axis=1)
        a_buffer = repeat(a_buffer, f"b l a -> b l {G} a")
        C = len(self.critics)
        # arrays used by critic update end up in a (B, L, C, G, dim) format
        assert batch.rews.shape == (B, L - 1, 1)
        assert batch.dones.shape == (B, L - 1, 1)
        r = repeat((self.reward_multiplier * batch.rews).float(), f"b l r -> b l 1 {G} r")
        d = repeat(batch.dones.float(), f"b l d -> b l 1 {G} d")
        D_emb = self.traj_encoder.emb_dim
        state_mask = (~((batch.rl2s == self.pad_val).all(-1, keepdim=True))).float()
        actor_mask = repeat(state_mask, f"b l 1 -> b l {G} 1")
        critic_mask = repeat(state_mask[:, 1:, ...], f"b l 1 -> b l {C} {G} 1")

        ################################
        ## Step 2: Sequence Embedding ##
        ################################
        # one trajectory encoder forward pass
        s_rep, hidden_state = self.traj_encoder(seq=o, time_idxs=batch.time_idxs, hidden_state=None)
        assert s_rep.shape == (B, L, D_emb)

        #########################################
        ## Step 3: a' ~ \pi, Q(s, a'), Q(s, a) ##
        #########################################
        critic_loss = None
        # one actor forward pass
        a_dist = self.actor(s_rep)

        if self.discrete:
            a_agent = a_dist.probs
        elif self.actor.actions_differentiable:
            a_agent = a_dist.rsample()
        else:
            a_agent = a_dist.sample()

        if not self.fake_filter or self.online_coeff > 0:
            s_a_agent_g = (s_rep.detach(), a_agent)
            # detach() above b/c these grads flow to traj_encoder through the policy
            s_a_g = (s_rep[:, :-1, ...], a_buffer[:, :-1, ...])
            # all the `phi` terms are only here b/c we used to implement DR3
            q_s_a_agent_g, phi_s_a_agent_g = self.maximized_critics(*s_a_agent_g)
            assert q_s_a_agent_g.shape == (B, L, C, G, 1)
            q_s_a_g, phi_s_a_g = self.critics(*s_a_g)

            ########################
            ## Step 4: TD Targets ##
            ########################
            # \mathcal{B}\bar{Q}(s, a, g)
            with torch.no_grad():
                a_prime_dist = self.target_actor(s_rep) if self.use_target_actor else a_dist
                ap = a_prime_dist.probs if self.discrete else a_prime_dist.sample()
                assert ap.shape == (B, L, G, D_action)
                sp_ap_gp = (s_rep[:, 1:, ...].detach(), ap[:, 1:, ...].detach())
                q_targ_sp_ap_gp, _ = self.target_critics(*sp_ap_gp)
                assert q_targ_sp_ap_gp.shape == (B, L - 1, C, G, 1)
                q_targ_sp_ap_gp = self.popart(q_targ_sp_ap_gp, normalized=False)
                assert q_targ_sp_ap_gp.shape == (B, L - 1, C, G, 1)
                gamma = self.gammas.to(r.device).unsqueeze(-1)
                ensemble_td_target = r + gamma * (1.0 - d) * q_targ_sp_ap_gp
                assert ensemble_td_target.shape == (B, L - 1, C, G, 1)
                # redq random subset of critic ensemble. multigamma format
                # makes this even more random.
                random_subset = torch.randint(
                    low=0,
                    high=C,
                    size=(B, L - 1, self.num_critics_td, G, 1),
                    device=r.device,
                )
                td_target_rand = torch.take_along_dim(
                    ensemble_td_target, random_subset, dim=2
                )
                if self.online_coeff > 0:
                    # clipped double q
                    td_target = td_target_rand.min(2, keepdims=True).values
                else:
                    # without DPG updates the usual min creates strong underestimation. take mean instead
                    td_target = td_target_rand.mean(2, keepdims=True)
                assert td_target.shape == (B, L - 1, 1, G, 1)
                self.popart.update_stats(
                    td_target, mask=critic_mask.all(2, keepdim=True)
                )
                td_target_norm = self.popart.normalize_values(td_target)
                assert td_target_norm.shape == (B, L - 1, 1, G, 1)

            #########################
            ## Step 5: Critic Loss ##
            #########################
            assert q_s_a_g.shape == (B, L - 1, C, G, 1)
            critic_loss = (self.popart(q_s_a_g) - td_target_norm.detach()).pow(2)
            assert critic_loss.shape == (B, L - 1, C, G, 1)
            if log_step:
                td_stats = self._td_stats(
                    critic_mask,
                    q_s_a_g,
                    self.popart(q_s_a_g, normalized=False),
                    r,
                    td_target,
                )
                popart_stats = self._popart_stats()
                self.update_info.update(td_stats | popart_stats)

        ######################
        ## Step 6: DPG Loss ##
        ######################
        actor_loss = torch.zeros((B, L - 1, G, 1), device=a.device)
        if self.online_coeff > 0:
            assert (
                self.actor.actions_differentiable
            ), "online-style actor loss is not compatible with action distribution"
            actor_loss += self.online_coeff * -(
                self.popart(q_s_a_agent_g[:, :-1, ...].min(2).values)
            )
        if log_step:
            self.update_info.update(self._policy_stats(actor_mask, a_dist))

        ######################
        ## Step 7: FBC Loss ##
        ######################
        if self.offline_coeff > 0:
            if not self.fake_filter:
                # Critic Regularized Regression (but noisy w/ k = 1 to save a forward pass)
                with torch.no_grad():
                    val_s_g = q_s_a_agent_g[:, :-1, ...].mean(2).detach()
                    assert val_s_g.shape == (B, L - 1, G, 1)
                    advantage_a_s_g = q_s_a_g.mean(2) - val_s_g
                    assert advantage_a_s_g.shape == (B, L - 1, G, 1)
                    filter_ = (advantage_a_s_g > 1e-3).float()
            else:
                # Behavior Cloning
                filter_ = torch.ones((1, 1, 1, 1), device=a.device)
            if self.discrete:
                # buffer actions are one-hot encoded
                logp_a = a_dist.log_prob(a_buffer.argmax(-1)).unsqueeze(-1)
            elif self.multibinary:
                logp_a = a_dist.log_prob(a_buffer).mean(-1, keepdim=True)
            else:
                # action probs at the [-1, 1] border can be unstable
                logp_a = a_dist.log_prob(a_buffer.clamp(-0.995, 0.995)).sum(-1, keepdim=True)
            # clamp for stability and throw away last action that was a duplicate
            logp_a = logp_a[:, :-1, ...].clamp(-1e3, 1e3)
            # filtered nll
            actor_loss += self.offline_coeff * -(filter_.detach() * logp_a)
            if log_step:
                filter_stats = self._filter_stats(actor_mask, logp_a, filter_)
                self.update_info.update(filter_stats)

        # fmt: on
        return critic_loss, actor_loss

    def _td_stats(self, mask, raw_q_s_a_g, q_s_a_g, r, td_target) -> dict:
        # messy data gathering for wandb console
        def masked_avg(x_, dim=0):
            return (mask[..., dim, :] * x_[..., dim, :]).sum().detach() / mask[
                ..., dim, :
            ].sum()

        stats = {}
        for i, gamma in enumerate(self.gammas):
            stats[f"Q(s, a) (global mean, rescaled) gamma={gamma:.3f}"] = masked_avg(
                q_s_a_g, i
            )
            stats[f"Q(s,a) (global mean, raw scale) gamma={gamma:.3f}"] = masked_avg(
                raw_q_s_a_g, i
            )
            stats[
                f"Q(s, a) Ensemble Stdev. (raw scale, ignoring padding) gamma={gamma:.3f}"
            ] = (raw_q_s_a_g[..., i, :].std(2).mean())

        stats.update(
            {
                "Q(s, a) (global std, rescaled, ignoring padding)": q_s_a_g.std(),
                "Min TD Target": td_target[
                    torch.where(mask.all(2, keepdims=True) > 0)
                ].min(),
                "Max TD Target": td_target[
                    torch.where(mask.all(2, keepdims=True) > 0)
                ].max(),
                "TD Target (test-time gamma)": masked_avg(td_target, -1),
                "Mean Reward (in training sequences)": masked_avg(r),
                "real_return": torch.flip(
                    torch.cumsum(torch.flip(mask.all(2, keepdims=True) * r, (1,)), 1),
                    (1,),
                ).squeeze(-1),
            }
        )
        return stats

    def _policy_stats(self, mask, a_dist) -> dict:
        # messy data gathering for wandb console
        # mask shape is batch length gammas 1
        sum_ = mask.sum((0, 1))
        masked_avg = (
            lambda x_, dim: (mask[..., dim, :] * x_[..., dim, :]).sum().detach() / sum_
        )

        if self.discrete:
            entropy = a_dist.entropy().unsqueeze(-1)
            low_prob = torch.min(a_dist.probs, dim=-1, keepdims=True).values
            high_prob = torch.max(a_dist.probs, dim=-1, keepdims=True).values
            return {
                "Policy Per-timestep Entropy (test-time gamma)": masked_avg(
                    entropy, -1
                ),
                "Policy Per-timstep Low Prob. (test-time gamma)": masked_avg(
                    low_prob, -1
                ),
                "Policy Per-timestep High Prob. (test-time gamma)": masked_avg(
                    high_prob, -1
                ),
                "Policy Overall Highest Prob.": (mask * a_dist.probs).max(),
            }
        else:
            entropy = -a_dist.log_prob(a_dist.sample()).sum(-1, keepdims=True)
            return {"Policy Entropy (test-time gamma)": masked_avg(entropy, -1)}

    def _filter_stats(self, mask, logp_a, filter_) -> dict:
        mask = mask[:, :-1, ...]

        # messy data gathering for wandb console
        def masked_avg(x_, dim=0):
            return (mask[..., dim, :] * x_[..., dim, :]).sum().detach() / mask[
                ..., dim, :
            ].sum()

        # messy data gathering for wandb console
        stats = {
            "Minimum Action Logprob": logp_a.min(),
            "Maximum Action Logprob": logp_a.max(),
            "Pct. of Actions Approved by Binary FBC Filter (All Gammas)": utils.masked_avg(
                filter_, mask
            )
            * 100.0,
        }

        if filter_.shape[-2] == len(self.gammas):
            for i, gamma in enumerate(self.gammas):
                stats[
                    f"Pct. of Actions Approved by Binary FBC (gamma = {gamma : .3f})"
                ] = (masked_avg(filter_, dim=i) * 100.0)
        return stats

    def _popart_stats(self) -> dict:
        # messy data gathering for wandb console
        return {
            "PopArt mu (mean over gamma)": self.popart.mu.data.mean().item(),
            "PopArt nu (mean over gamma)": self.popart.nu.data.mean().item(),
            "PopArt w (mean over gamma)": self.popart.w.data.mean().item(),
            "PopArt b (mean over gamma)": self.popart.b.data.mean().item(),
            "PopArt sigma (mean over gamma)": self.popart.sigma.mean().item(),
        }

    def add_data_to_sequence_buffer(
            self,
            obs: np.ndarray,
            time_step: int,
            done: bool,
            obs_sequence: GPUSequenceBuffer,
            goal_sequence: GPUSequenceBuffer,
            rl2_sequence: GPUSequenceBuffer,
            action: np.ndarray = None,
            reward: float = None
    ):
        # the expand dims shaping below is to align with the
        # (num_workers, 1, ...) shape of the sequence buffers

        # for some reason Amago wants the obs stored as a dict
        _obs = {
            "observation": np.expand_dims(obs, axis=(0, 1))
        }
        _done = np.expand_dims(done, axis=(0, 1))
        _goal = np.expand_dims(np.array([0.], dtype=np.float32), axis=(0, 1, 2))
        _time = np.expand_dims(np.array([time_step], dtype=np.float32), axis=(0, 1))

        # first timestep requires some dummy variables
        if time_step == 0:
            _action = np.expand_dims(np.array([-2] * self.action_dim, dtype=np.float32), axis=(0, 1))  # -2 for dummy action, 0
            _reset = np.expand_dims(np.array([1.], dtype=np.float32), axis=(0, 1))
            _reward = np.expand_dims(np.array([0.], dtype=np.float32), axis=(0, 1))
        else:
            _action = np.expand_dims(action, axis=(0))
            _reset = np.expand_dims(np.array([0.], dtype=np.float32), axis=(0, 1))
            _reward = np.expand_dims([reward], axis=(0, 1))

        _rl2 = np.concatenate((_reset, _reward, _time, _action), axis=-1, dtype=np.float32)

        # add to sequence buffers
        obs_sequence.add_timestep(_obs, _done)
        goal_sequence.add_timestep(_goal, _done)
        rl2_sequence.add_timestep(_rl2, _done)

        return obs_sequence, goal_sequence, rl2_sequence


@gin.configurable
def binary_filter(adv, threshold: float = 0.0):
    """
    Many results in the second paper use a `threshold` of -1e-4 (instead of 0),
    which sometimes helps stability in sparse reward envs, but defaulting
    to it was a version control mistake. This would never matter when
    using scalar output critics but *does* matter when using classification
    two-hot critics with many bins where advantages are often close to zero.
    """
    return adv > threshold


@gin.configurable
class MultiTaskAgent(Agent):
    def __init__(
        self,
        obs_space: gym.spaces.Dict,
        goal_space: gym.spaces.Box,
        rl2_space: gym.spaces.Box,
        action_space: gym.spaces.Space,
        max_seq_len: int,
        horizon: int,
        online_coeff: float = 0.0,
        offline_coeff: float = 1.0,
        fbc_filter_k: int = 3,
        fbc_filter_func: callable = binary_filter,
        **kwargs,
    ):
        super().__init__(
            obs_space=obs_space,
            goal_space=goal_space,
            rl2_space=rl2_space,
            action_space=action_space,
            max_seq_len=max_seq_len,
            horizon=horizon,
            online_coeff=online_coeff,
            offline_coeff=offline_coeff,
            **kwargs,
        )
        self.fbc_filter_k = fbc_filter_k
        self.fbc_filter_func = fbc_filter_func

        critic_kwargs = {
            "state_dim": self.traj_encoder.emb_dim,
            "action_dim": self.action_dim,
            "gammas": self.gammas,
            "num_critics": self.num_critics,
        }
        self.critics = actor_critic.NCriticsTwoHot(**critic_kwargs)
        self.target_critics = actor_critic.NCriticsTwoHot(**critic_kwargs)
        self.maximized_critics = actor_critic.NCriticsTwoHot(**critic_kwargs)
        self.hard_sync_targets()

    def forward(self, batch: Batch, log_step: bool):
        # fmt: off
        self.update_info = {}  # holds wandb stats
        active_log_dict = self.update_info if log_step else None

        ##########################
        ## Step 0: Timestep Emb ##
        ##########################
        o = self.tstep_encoder(
            obs=batch.obs, goals=batch.goals, rl2s=batch.rl2s, log_dict=active_log_dict
        )

        ###########################
        ## Step 1: Get Organized ##
        ###########################
        B, L, D_o = o.shape
        a = batch.actions
        a = a.clamp(0, 1.0) if self.discrete else a.clamp(-1.0, 1.0)
        _B, _L, D_action = a.shape
        assert _L == L - 1
        G = len(self.gammas)
        a_buffer = torch.cat((a, a[:, -1, ...].clone().unsqueeze(1)), axis=1)
        a_buffer = repeat(a_buffer, f"b l a -> b l {G} a")
        C = len(self.critics)
        assert batch.rews.shape == (B, L - 1, 1)
        assert batch.dones.shape == (B, L - 1, 1)
        r = repeat(
            (self.reward_multiplier * batch.rews).float(), f"b l r -> b l 1 {G} r"
        )
        d = repeat(batch.dones.float(), f"b l d -> b l 1 {G} d")
        D_emb = self.traj_encoder.emb_dim
        Bins = self.critics.num_bins
        state_mask = (~((batch.rl2s == self.pad_val).all(-1, keepdim=True))).float()
        actor_mask = repeat(state_mask, f"b l 1 -> b l {G} 1")
        critic_mask = repeat(state_mask[:, 1:, ...], f"b l 1 -> b l {C} {G} 1")

        ################################
        ## Step 2: Sequence Embedding ##
        ################################
        s_rep, hidden_state = self.traj_encoder(
            seq=o, time_idxs=batch.time_idxs, hidden_state=None
        )
        assert s_rep.shape == (B, L, D_emb)

        #########################################
        ## Step 3: a' ~ \pi, Q(s, a'), Q(s, a) ##
        #########################################
        a_dist = self.actor(s_rep)
        if self.discrete:
            a_dist = actor_critic.DiscreteLikeContinuous(a_dist)

        critic_loss = None
        if not self.fake_filter or self.online_coeff > 0:
            ########################
            ## Step 4: TD Targets ##
            ########################
            with torch.no_grad():
                if self.use_target_actor:
                    a_prime_dist = self.target_actor(s_rep)
                    if self.discrete:
                        a_prime_dist = actor_critic.DiscreteLikeContinuous(a_prime_dist)
                else:
                    a_prime_dist = a_dist
                ap = a_prime_dist.sample()
                assert ap.shape == (B, L, G, D_action)
                sp_ap_gp = (s_rep[:, 1:, ...].detach(), ap[:, 1:, ...].detach())
                q_targ_sp_ap_gp, _ = self.target_critics(*sp_ap_gp)
                assert q_targ_sp_ap_gp.probs.shape == (B, L - 1, C, G, Bins)
                q_targ_sp_ap_gp = self.target_critics.bin_dist_to_raw_vals(
                    q_targ_sp_ap_gp
                )
                assert q_targ_sp_ap_gp.shape == (B, L - 1, C, G, 1)
                gamma = self.gammas.to(r.device).unsqueeze(-1)
                ensemble_td_target = r + gamma * (1.0 - d) * q_targ_sp_ap_gp
                assert ensemble_td_target.shape == (B, L - 1, C, G, 1)
                random_subset = torch.randint(
                    low=0,
                    high=C,
                    size=(B, L - 1, self.num_critics_td, G, 1),
                    device=r.device,
                )
                td_target_rand = torch.take_along_dim(
                    ensemble_td_target, random_subset, dim=2
                )
                td_target = td_target_rand.mean(2, keepdims=True)
                assert td_target.shape == (B, L - 1, 1, G, 1)
                # we are only using popart to track stats for online actor update,
                # since scale intentionally does not impact critic loss
                self.popart.update_stats(
                    td_target, mask=critic_mask.all(2, keepdim=True)
                )
                assert td_target.shape == (B, L - 1, 1, G, 1)
                td_target_labels = self.target_critics.raw_vals_to_labels(td_target)
                td_target_labels = repeat(
                    td_target_labels, f"b l 1 g bins -> b l {C} g bins"
                )
                assert td_target_labels.shape == (B, L - 1, C, G, Bins)

            #########################
            ## Step 5: Critic Loss ##
            #########################
            s_a_g = (s_rep, a_buffer)
            q_s_a_g, _ = self.critics(*s_a_g)
            assert q_s_a_g.probs.shape == (B, L, C, G, Bins)
            critic_loss = F.cross_entropy(
                rearrange(q_s_a_g.logits[:, :-1, ...], "b l c g u -> (b l c g) u"),
                rearrange(td_target_labels, "b l c g u -> (b l c g) u"),
                reduction="none",
            )
            critic_loss = rearrange(
                critic_loss, "(b l c g) -> b l c g 1", b=B, l=L - 1, c=C, g=G
            )
            assert critic_loss.shape == (B, L - 1, C, G, 1)
            if log_step:
                raw_q_s_a_g_ = self.critics.bin_dist_to_raw_vals(q_s_a_g)[:, :-1, ...]
                td_stats = self._td_stats(
                    critic_mask,
                    raw_q_s_a_g_,
                    self.popart.normalize_values(raw_q_s_a_g_),
                    r,
                    td_target,
                    raw_q_bins=q_s_a_g.probs[:, :-1],
                )
                popart_stats = self._popart_stats()
                self.update_info.update(td_stats | popart_stats)

        ######################
        ## Step 6: FBC Loss ##
        ######################
        actor_loss = 0.0
        if not self.fake_filter and self.offline_coeff > 0:
            with torch.no_grad():
                K = self.fbc_filter_k
                a_agent = rearrange(a_dist.sample((K,)), "k b l g a -> (b k) l g a")
                s_rep_fbc = repeat(s_rep.detach(), "b l f -> (b k) l f", k=K)
                s_a_agent = (s_rep_fbc, a_agent)
                q_s_a_agent, phi_s_a_agent = self.critics(*s_a_agent)
                assert q_s_a_agent.probs.shape == (B * K, L, C, G, Bins)
                val_s = self.critics.bin_dist_to_raw_vals(q_s_a_agent).mean(2).detach()
                assert val_s.shape == (B * K, L, G, 1)
                val_s = rearrange(val_s, "(b k) l g 1 -> b k l g 1", k=K).mean(1)
                assert val_s.shape == (B, L, G, 1)
                q_s_a_g = self.critics.bin_dist_to_raw_vals(q_s_a_g).mean(2)
                advantage_s_a = q_s_a_g - val_s
                assert advantage_s_a.shape == (B, L, G, 1)
                filter_ = self.fbc_filter_func(advantage_s_a)[:, :-1, ...].float()
                binary_filter_ = binary_filter(advantage_s_a)[:, :-1, ...].float()
        else:
            filter_ = binary_filter_ = torch.ones(
                (B, L - 1, G, 1), dtype=torch.float32, device=s_rep.device
            )

        if self.offline_coeff > 0:
            if self.discrete:
                logp_a = a_dist.log_prob(a_buffer)
            elif self.multibinary:
                logp_a = a_dist.log_prob(a_buffer).mean(-1, keepdim=True)
            else:
                logp_a = a_dist.log_prob(a_buffer.clamp(-0.995, 0.995)).sum(
                    -1, keepdim=True
                )
            logp_a = logp_a[:, :-1, ...].clamp(-1e3, 1e3)
            actor_loss += self.offline_coeff * -(filter_.detach() * logp_a)
            if log_step:
                policy_stats = self._policy_stats(actor_mask, a_dist)
                filter_stats = self._filter_stats(actor_mask, logp_a, binary_filter_)
                self.update_info.update(filter_stats | policy_stats)

        ######################
        ## Step 7: DPG Loss ##
        ######################
        if self.online_coeff > 0:
            assert (
                self.actor.actions_differentiable and not self.discrete
            ), "this ablation only supports continuous actions with rsample()"
            a_agent = a_dist.rsample()
            q_s_a_agent, _ = self.maximized_critics(s_rep.detach(), a_agent)
            q_s_a_agent = self.popart.normalize_values(
                self.maximized_critics.bin_dist_to_raw_vals(q_s_a_agent).min(2).values
            )
            actor_loss += self.online_coeff * -(q_s_a_agent[:, :-1, ...])

        return critic_loss, actor_loss

    def _td_stats(self, mask, raw_q_s_a_g, q_s_a_g, r, td_target, raw_q_bins) -> dict:
        stats = super()._td_stats(
            mask=mask,
            raw_q_s_a_g=raw_q_s_a_g,
            q_s_a_g=q_s_a_g,
            r=r,
            td_target=td_target,
        )
        *_, Bins = raw_q_bins.shape
        max_bin_all_gammas_histogram = raw_q_bins.argmax(-1)[torch.where(mask.all(-1))]
        max_bin_target_gamma_histogram = raw_q_bins[..., -1, :].argmax(-1)[
            torch.where(mask[..., -1, :].all(-1))
        ]
        stats.update(
            {
                "Maximum Bin (All Gammas)": wandb.Histogram(
                    max_bin_all_gammas_histogram.cpu().numpy(), num_bins=Bins
                ),
                "Maximum Bin (Target Gamma)": wandb.Histogram(
                    max_bin_target_gamma_histogram.cpu().numpy(), num_bins=Bins
                ),
            }
        )
        return stats
