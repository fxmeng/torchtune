# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import math
from typing import List, Optional, Union

import torch
import torch.nn.functional as F

from torch import nn

from torchao.dtypes.nf4tensor import linear_nf4, to_nf4
from torchtune.modules.low_precision import _register_nf4_dispatch_ops  # noqa: F401
from torchtune.modules.peft import AdapterModule


class PiSSALinear(nn.Module, AdapterModule):
    """PiSSA linear layer as introduced in `PiSSA: Principal Singular Values and Singular Vectors Adaptation of Large Language Models <https://arxiv.org/abs/2404.02948>`_.

    PiSSA perturbs a given layer via a low-rank approximation where only
    the rank decomposition matrices are trainable. In a linear layer instead of
    :math:`x \\mapsto W_0x` a PiSSALinear layer is defined as
    :math:`x \\mapsto W_0x + (\\alpha / r)BAx`, where :math:`r` is the rank of
    the matrices :math:`A` and :math:`B` and :math:`\\alpha` is a scaling factor.
    As in the original implementation, we support dropout before multiplication
    by the low-rank matrices.

    Args:
        in_dim (int): input dimension
        out_dim (int): output dimension
        rank (int): rank of the low-rank approximation
        alpha (float): scaling factor for the low-rank approximation
        dropout (float): dropout probability. Default: 0.0
        use_bias (bool): whether to include bias in the original linear layer.
            Default: False
        quantize_base (bool): Whether to quantize base linear weight or not.
            Default: False
        **quantization_kwargs: Keyword arguments to pass to `to_nf4` when quantizing the base linear weight.
            Examples of valid arguments are `block_size` and `scaler_block_size`, which control the granularity of
            weight quantization and scaler quantization respectively. This is only used if `quantize_base` is True.
            Default None

    Raises:
        ValueError: If ``quantize_base`` is False, but quantization kwargs are provided.
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        rank: int,
        alpha: float,
        dropout: float = 0.0,
        use_bias: bool = False,
        quantize_base: bool = False,
        **quantization_kwargs,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.rank = rank
        self.alpha = alpha
        self.use_bias = use_bias
        self._quantize_base = quantize_base

        if not self._quantize_base and any([v for v in quantization_kwargs.values()]):
            raise ValueError(
                f"``quantize_base`` is False, but received the following quantization arguments: {quantization_kwargs}"
            )

        # Setup weight and bias
        linear = nn.Linear(in_features=in_dim, out_features=out_dim, bias=self.use_bias)
        weight = (
            linear.weight
            if not self._quantize_base
            else to_nf4(linear.weight, **quantization_kwargs)
        )
        bias = linear.bias if self.use_bias else None

        # 'self.disabled' is a flag showing whether to turn off PiSSA adapters,
        # this can be used in DPO for treating the lora adapters as the policy model
        # and disabling it to treat the base model as the reference model
        self.disabled = False
        self.register_parameter("weight", nn.Parameter(weight))
        self.register_parameter(
            "bias", nn.Parameter(bias) if bias is not None else None
        )
        self.dropout = nn.Dropout(p=dropout) if dropout > 0.0 else nn.Identity()
        self.pissa_u = nn.Linear(in_features=in_dim, out_features=rank, bias=False)
        self.pissa_s = nn.Parameter(torch.zeros(rank))
        self.pissa_v = nn.Linear(in_features=rank, out_features=out_dim, bias=False)
        self.merged = False
        self.initialize_parameters()

    def to_empty(
        self, *, device: Optional[Union[str, torch.device, int]], recurse: bool = True
    ):
        self.pissa_u.to_empty(device=device, recurse=recurse)
        pissa_s = nn.Parameter(
            torch.empty_like(self.pissa_s, device=device),
            requires_grad=self.pissa_s.requires_grad,
        )
        torch.utils.swap_tensors(self.pissa_s, pissa_s)
        self.pissa_v.to_empty(device=device, recurse=recurse)

    def initialize_parameters(self):
        # Initialize as in
        # https://github.com/microsoft/PiSSA/blob/4c0333854cb905966f8cc4e9a74068c1e507c7b7/loralib/layers.py#L119
        _pissa_u_init_params(self.pissa_u)
        _pissa_s_init_params(self.pissa_s)
        _pissa_v_init_params(self.pissa_v)

    @torch.no_grad()
    def initialize_pissa(self, fsvd_niter=None):
        """
        PiSSA initializes the magnitude vector such that its outputs are initially
        identical to standard LoRA's outputs.

        This must be called after loading/initializing base model and LoRA params.

        Raises:
            RuntimeError: If base or LoRA parameters are still on meta device.
        """
        if any(
            [
                self.weight.is_meta,
                self.pissa_u.weight.is_meta,
                self.pissa_s.is_meta,
                self.pissa_v.weight.is_meta,
            ]
        ):
            raise RuntimeError(
                "Cannot initialize PiSSA if base or LoRA parameters are still on meta device."
            )
        dtype =  self.weight.dtype
        base_weight = self.weight.to(torch.float32)
        if fsvd_niter is not None:
            if isinstance(fsvd_niter, int) and fsvd_niter >= 0:
                Vr, Sr, Ur = torch.svd_lowrank(base_weight, self.rank, niter=fsvd_niter)
                Sr /= self.alpha / self.rank
                Uhr = Ur.t()
            else:
                raise RuntimeError(
                    "Fast SVD niter is a non-negative integer. It is recommended to set it equal to the rank."
                )
        else:
            V, S, Uh = torch.linalg.svd(base_weight, full_matrices=False)
            Vr = V[:, : self.rank]
            Sr = S[: self.rank]
            Sr /= self.alpha / self.rank
            Uhr = Uh[: self.rank]
        
        self.pissa_u.weight.copy_(Uhr)
        self.pissa_s.copy_(Sr)
        self.pissa_v.weight.copy_(Vr)
        residual_weight = base_weight - Vr @ torch.diag(Sr) @ Uhr
        self.weight.copy_(residual_weight.to(dtype))
        
    def adapter_params(self) -> List[str]:
        """
        Return a list of strings corresponding to the names of the ``nn.Parameter`` s in
        the model coming from the adapter.

        For PiSSA this means pissa_u.weight, pissa_s and pissa_v.weight.
        """
        # NOTE: this function has to be updated if the names of "pissa_u" and "pissa_v"
        # in this module change.
        adapter_params = ["pissa_u.weight", "pissa_s", "pissa_v.weight"]
        return adapter_params

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x (torch.Tensor): input tensor with shape ``(..., in_dim)``

        Returns:
            torch.Tensor: output tensor with shape ``(..., out_dim)``

        """
        if self._quantize_base:
            out = linear_nf4(input=x, weight=self.weight)
            if self.use_bias:
                out = out + self.bias
        else:
            out = F.linear(x, self.weight, self.bias)
        if self.disabled:
            return out
        lora_out = self.pissa_u(self.dropout(x))
        lora_out = lora_out * self.pissa_s * (self.alpha / self.rank)
        lora_out = self.pissa_v(lora_out)
        return out + lora_out

class QATPiSSALinear(PiSSALinear):
    """
    PiSSA linear layer with quantization-aware training (QAT) applied to the
    activations and/or weights before the low rank adapters.

    QAT leverages fake quantization to simulate the quantization numerics during
    training without actually casting the data to lower precision. This class
    combines PiSSA with QAT to improve the final quantized accuracy during inference
    while reducing the memory required during training.

    Args:
        in_dim (int): input dimension
        out_dim (int): output dimension
        rank (int): rank of the low-rank approximation
        alpha (float): scaling factor for the low-rank approximation
        dropout (float): dropout probability. Default: 0.0
        activation_qat_config (Optional[FakeQuantizeConfig]): config for specifying
            how input activations will be fake quantized, defaults to None
        weight_qat_config (Optional[FakeQuantizeConfig]): config for specifying
            how weights will be fake quantized, defaults to None

    Raises:
        ValueError: If `in_dim` is not divisible by weight `group_size`

    Example usage::

        activation_qat_config = FakeQuantizeConfig(
            dtype=torch.int8,
            granularity="per_token",
            is_symmetric=False,
        )
        weight_qat_config = FakeQuantizeConfig(
            dtype=torch.int4,
            group_size=8,
            is_symmetric=True,
        )
        qat_lora_linear = QATPiSSALinear(
            in_dim=512,
            out_dim=1024,
            rank=8,
            alpha=16,
            dropout=0.0,
            activation_qat_config=activation_qat_config,
            weight_qat_config=weight_qat_config,
        )
        qat_lora_linear(torch.randn(512))
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        rank: int,
        alpha: float,
        dropout: float = 0.0,
        # fake quantize configs
        # TODO: make the types Optional[FakeQuantizeConfig] once we
        # support torchao 0.7+ by default
        activation_qat_config: Optional["FakeQuantizeConfig"] = None,
        weight_qat_config: Optional["FakeQuantizeConfig"] = None,
    ):
        super().__init__(
            in_dim,
            out_dim,
            rank,
            alpha,
            dropout,
            use_bias=False,
            quantize_base=False,
        )

        try:
            from torchao.quantization.qat.api import FakeQuantizeConfig
            from torchao.quantization.qat.fake_quantizer import FakeQuantizer
        except ImportError as err:
            raise ValueError(
                "QATPiSSALinear is only compatible with torchao 0.7+"
            ) from err

        # initialize activation fake quantizer
        if activation_qat_config is not None:
            assert isinstance(activation_qat_config, FakeQuantizeConfig)
            self.activation_fake_quantizer = FakeQuantizer(activation_qat_config)
        else:
            self.activation_fake_quantizer = nn.Identity()

        # initialize weight fake quantizer
        if weight_qat_config is not None:
            assert isinstance(weight_qat_config, FakeQuantizeConfig)
            group_size = weight_qat_config.group_size
            if group_size is not None and in_dim % group_size != 0:
                raise ValueError(
                    "in_dim (%s) must be divisible by group_size (%s)"
                    % (in_dim, group_size)
                )
            self.weight_fake_quantizer = FakeQuantizer(weight_qat_config)
        else:
            self.weight_fake_quantizer = nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x (torch.Tensor): input tensor with shape ``(..., in_dim)``

        Returns:
            torch.Tensor: output tensor with shape ``(..., out_dim)``

        """
        _x = self.activation_fake_quantizer(x)
        w = self.weight_fake_quantizer(self.weight)
        out = F.linear(_x, w)
        if self.disabled:
            return out
        lora_out = self.pissa_u(self.dropout(x))
        lora_out = lora_out * self.pissa_s * (self.alpha / self.rank)
        lora_out = self.pissa_v(lora_out)
        return out + lora_out

    @classmethod
    def from_lora_linear(
        cls,
        lora_linear: PiSSALinear,
        # TODO: make the types Optional[FakeQuantizeConfig] once we
        # support torchao 0.7+ by default
        activation_qat_config: Optional["FakeQuantizeConfig"] = None,
        weight_qat_config: Optional["FakeQuantizeConfig"] = None,
    ) -> "QATPiSSALinear":
        """
        Create a `QATPiSSALinear` from an existing `PiSSALinear`,
        preserving the weights and adapters.
        """
        if lora_linear.bias is not None:
            ValueError("Bias is not supported in QAT + PiSSA yet")
        if lora_linear._quantize_base:
            ValueError("quantize_base is not compatible with QAT + PiSSA")
        if isinstance(lora_linear.dropout, nn.Dropout):
            dropout = lora_linear.dropout.p
        else:
            dropout = 0.0
        new_linear = cls(
            lora_linear.in_dim,
            lora_linear.out_dim,
            lora_linear.rank,
            lora_linear.alpha,
            dropout,
            activation_qat_config,
            weight_qat_config,
        )
        # In distributed training, the model may be instantiated
        # on the meta device, in which case there is no need to
        # copy the weights, and doing so will result in an error
        if lora_linear.weight.device != torch.device("meta"):
            new_linear.weight = lora_linear.weight
        if lora_linear.pissa_u.weight.device != torch.device("meta"):
            new_linear.pissa_u.weight = lora_linear.pissa_u.weight
        if lora_linear.pissa_v.weight.device != torch.device("meta"):
            new_linear.pissa_v.weight = lora_linear.pissa_v.weight
        return new_linear

def _pissa_u_init_params(x: nn.Linear) -> None:
    """
    Initialize PiSSA U weight to Kaiming uniform.
    """
    nn.init.kaiming_uniform_(x.weight, a=math.sqrt(5))

def _pissa_s_init_params(x: nn.Parameter) -> None:
    """
    Initialize PiSSA S weight to zeros.
    """
    nn.init.zeros_(x)
    
def _pissa_v_init_params(x: nn.Linear) -> None:
    """
    Initialize PiSSA V weight to zeros.
    """
    nn.init.kaiming_uniform_(x.weight, a=math.sqrt(5))
