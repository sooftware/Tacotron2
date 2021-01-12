# Copyright (c) 2020, Soohwan Kim. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from tacotron2.modules import Linear
from typing import Tuple


class LocationSensitiveAttention(nn.Module):
    """
    This attention encourages the model to move forward consistently through the input,
    mitigating potential failure modes where some subsequences are repeated or ignored by the decoder.

    Args:
        lstm_hidden_dim: dimension of rnn hidden state vector (default: 1024)
        embedding_dim: dimension of character embedding layer (default: 512)
        attn_dim: dimension of attention (default: 128)
        location_conv_filter_size: size of location convolution layer`s filter (default: 32)
        location_conv_kernel_size: size of location convolution layer`s kernel (default: 31)

    Inputs:
        - **query** (batch, q_len, hidden_dim): tensor containing the output features from the decoder.
        - **value** (batch, v_len, hidden_dim): tensor containing features of the encoded input sequence.
        - **last_alignment_energy** (batch_size, v_len): tensor containing previous time step`s attention (alignment)

    Returns: context_vector, alignment_energy
        - **context_vector** (batch, output_len, dimensions): tensor containing the feature from encoder outputs
        - **alignment_energy** (batch , v_len): tensor containing the attention (alignment) from the encoder outputs.
    """
    def __init__(
            self,
            lstm_hidden_dim: int = 1024,             # dimension of rnn hidden state vector
            embedding_dim: int = 512,                # dimension of character embedding layer
            attn_dim: int = 128,                     # dimension of attention
            location_conv_filter_size: int = 32,     # size of location convolution layer`s filter
            location_conv_kernel_size: int = 31      # size of location convolution layer`s kernel
    ) -> None:
        super(LocationSensitiveAttention, self).__init__()
        self.attn_dim = attn_dim
        self.query_proj = Linear(lstm_hidden_dim, attn_dim, bias=False)
        self.value_proj = Linear(embedding_dim, attn_dim, bias=False)
        self.align_proj = Linear(attn_dim, 1, bias=True)
        self.bias = nn.Parameter(torch.rand(attn_dim).uniform_(-0.1, 0.1))

        self.location_conv = nn.Conv1d(
            in_channels=2,
            out_channels=location_conv_filter_size,
            kernel_size=location_conv_kernel_size,
            padding=int((location_conv_kernel_size - 1) / 2),
            bias=False
        )
        self.location_proj = Linear(location_conv_filter_size, attn_dim, bias=False)

    def forward(self, query: Tensor, value: Tensor, last_alignment: Tensor) -> Tuple[Tensor, Tensor]:
        batch_size = query.size(0)
        query = query.unsqueeze(1)

        last_alignment = self.location_conv(last_alignment)
        last_alignment = last_alignment.transpose(1, 2)
        last_alignment = self.location_proj(last_alignment)

        alignment = self.align_proj(torch.tanh(
            self.query_proj(query.reshape(-1, query.size(2))).view(batch_size, -1, self.attn_dim)
            + self.value_proj(value.reshape(-1, value.size(2))).view(batch_size, -1, self.attn_dim)
            + last_alignment
            + self.bias
        )).squeeze(-1)
        alignment = F.softmax(alignment, dim=-1)

        context = torch.bmm(alignment.unsqueeze(1), value)
        context = context.squeeze(1)

        return context, alignment
