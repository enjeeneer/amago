from abc import ABC, abstractmethod
from typing import Optional
import math

import torch
from torch import nn
import gin

from amago.nets.goal_embedders import FFGoalEmb, TokenGoalEmb
from amago.nets.utils import InputNorm, add_activation_log, symlog
from amago.nets import ff, cnn


@gin.configurable
class TstepEncoder(nn.Module, ABC):
    def __init__(self, obs_space, goal_space, rl2_space, goal_emb_Cls=TokenGoalEmb):
        super().__init__()
        self.obs_space = obs_space
        self.goal_space = goal_space
        self.rl2_space = rl2_space
        goal_length, goal_dim = goal_space.shape
        self.goal_emb = goal_emb_Cls(goal_length=goal_length, goal_dim=goal_dim)
        self.goal_emb_dim = self.goal_emb.goal_emb_dim

    def forward(self, obs, goals, rl2s, log_dict: Optional[dict] = None):
        goal_rep = self.goal_emb(goals)
        out = self.inner_forward(obs, goal_rep, rl2s, log_dict=log_dict)
        return out

    @abstractmethod
    def inner_forward(self, obs, goal_rep, rl2s, log_dict: Optional[dict] = None):
        pass

    @property
    @abstractmethod
    def emb_dim(self):
        pass


@gin.configurable
class FFTstepEncoder(TstepEncoder):
    def __init__(
        self,
        obs_space,
        goal_space,
        rl2_space,
        device,
        n_layers: int = 2,
        d_hidden: int = 512,
        d_output: int = 256,
        norm: str = "layer",
        activation: str = "leaky_relu",
        hide_rl2s: bool = False,
        normalize_inputs: bool = True,
    ):
        super().__init__(
            obs_space=obs_space, goal_space=goal_space, rl2_space=rl2_space
        )
        flat_obs_shape = math.prod(self.obs_space["observation"].shape)
        in_dim = flat_obs_shape + self.goal_emb_dim + self.rl2_space.shape[-1]
        self.in_norm = InputNorm(
            flat_obs_shape + self.rl2_space.shape[-1], skip=not normalize_inputs
        ).to(device)
        self.base = ff.MLP(
            d_inp=in_dim,
            d_hidden=d_hidden,
            n_layers=n_layers,
            d_output=d_output,
            activation=activation,
        ).to(device)
        self.out_norm = ff.Normalization(norm, d_output).to(device)
        self._emb_dim = d_output
        self.hide_rl2s = hide_rl2s

    def inner_forward(self, obs, goal_rep, rl2s, log_dict: Optional[dict] = None):
        # multi-modal envs that do not use the default `observation` key need their own custom encoders.
        obs = obs["observation"]
        B, L, *_ = obs.shape
        if self.hide_rl2s:
            rl2s = rl2s * 0
        flat_obs_rl2 = torch.cat((obs.view(B, L, -1).float(), rl2s), dim=-1)
        if self.training:
            self.in_norm.update_stats(flat_obs_rl2)
        flat_obs_rl2 = self.in_norm(flat_obs_rl2)
        obs_rl2_goals = torch.cat((flat_obs_rl2, goal_rep), dim=-1)
        prenorm = self.base(obs_rl2_goals)
        out = self.out_norm(prenorm)
        return out

    @property
    def emb_dim(self):
        return self._emb_dim


@gin.configurable
class CNNTstepEncoder(TstepEncoder):
    def __init__(
        self,
        obs_space,
        goal_space,
        rl2_space,
        cnn_Cls=cnn.NatureishCNN,
        channels_first: bool = False,
        img_features: int = 384,
        rl2_features: int = 12,
        d_output: int = 384,
        out_norm: str = "layer",
        activation: str = "leaky_relu",
        skip_rl2_norm: bool = False,
        hide_rl2s: bool = False,
        drqv2_aug: bool = True,
    ):
        super().__init__(
            obs_space=obs_space, goal_space=goal_space, rl2_space=rl2_space
        )
        self.data_aug = (
            cnn.DrQv2Aug(4, channels_first=channels_first) if drqv2_aug else lambda x: x
        )
        obs_shape = self.obs_space["observation"].shape
        self.cnn = cnn_Cls(
            img_shape=obs_shape,
            channels_first=channels_first,
            activation=activation,
        )
        img_feature_dim = self.cnn(
            torch.zeros((1, 1) + obs_shape, dtype=torch.uint8)
        ).shape[-1]
        self.img_features = nn.Linear(img_feature_dim, img_features)

        self.rl2_norm = InputNorm(self.rl2_space.shape[-1], skip=skip_rl2_norm)
        self.rl2_features = nn.Linear(rl2_space.shape[-1], rl2_features)

        mlp_in = img_features + self.goal_emb_dim + rl2_features
        self.merge = nn.Linear(mlp_in, d_output)
        self.out_norm = ff.Normalization(out_norm, d_output)
        self.hide_rl2s = hide_rl2s
        self._emb_dim = d_output

    def inner_forward(self, obs, goal_rep, rl2s, log_dict: Optional[dict] = None):
        # multi-modal envs that do not use the default `observation` key need their own custom encoders.
        img = obs["observation"].float()
        B, L, *_ = img.shape
        if self.training:
            og_split = max(min(math.ceil(B * 0.25), B - 1), 0)
            aug = self.data_aug(img[og_split:, ...])
            img = torch.cat((img[:og_split, ...], aug), dim=0)
        img = (img / 128.0) - 1.0
        img_rep = self.cnn(img, flatten=True, from_float=True)
        add_activation_log("cnn_out", img_rep, log_dict)
        img_rep = self.img_features(img_rep)
        add_activation_log("img_features", img_rep, log_dict)

        rl2s = symlog(rl2s)
        rl2s_norm = self.rl2_norm(rl2s)
        if self.training:
            self.rl2_norm.update_stats(rl2s)
        if self.hide_rl2s:
            rl2s_norm = rl2s_norm * 0
        rl2s_rep = self.rl2_features(rl2s_norm)

        inp = torch.cat((img_rep, goal_rep, rl2s_rep), dim=-1)
        merge = self.merge(inp)
        add_activation_log("tstep_encoder_prenorm", merge, log_dict)
        out = self.out_norm(merge)
        return out

    @property
    def emb_dim(self):
        return self._emb_dim
