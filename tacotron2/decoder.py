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

import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from tacotron2.modules import Linear
from typing import Optional, Tuple, Dict, Any
from tacotron2.attention import LocationSensitiveAttention
from tacotron2.prenet import PreNet


class Decoder(nn.Module):
    """
    The decoder is an autoregressive recurrent neural network which predicts
    a mel spectrogram from the encoded input sequence one frame at a time.

    Args:
        num_mel_bins: number of mel filters
        prenet_dim: dimension of prenet
        decoder_lstm_dim: dimension of decoder lstm network
        attn_lstm_dim: dimension of attention lstm network
        embedding_dim: dimension of embedding network
        attn_dim: dimension of attention layer
        location_conv_filter_size: size of location convolution filter
        location_conv_kernel_size: size of location convolution kernel
        prenet_dropout_p: dropout probability of prenet
        attn_dropout_p: dropout probability of attention network
        decoder_dropout_p: dropout probability of decoder network
        max_decoding_step: max decoding step
        stop_threshold: stop threshold

    Inputs:
        - **encoder_outputs**: tensor containing the encoded features of the input character sequences
        - **inputs**: target mel-spectrogram for training

    Returns:
        - **output**: dictionary contains mel_outputs, stop_outputs, alignments
    """
    def __init__(
            self,
            num_mel_bins: int = 80,                     # number of mel filters
            prenet_dim: int = 256,                      # dimension of prenet
            decoder_lstm_dim: int = 1024,               # dimension of decoder lstm network
            attn_lstm_dim: int = 1024,                  # dimension of attention lstm network
            embedding_dim: int = 512,                   # dimension of embedding network
            attn_dim: int = 128,                        # dimension of attention layer
            location_conv_filter_size: int = 32,        # size of location convolution filter
            location_conv_kernel_size: int = 31,        # size of location convolution kernel
            prenet_dropout_p: float = 0.5,              # dropout probability of prenet
            attn_dropout_p: float = 0.1,                # dropout probability of attention network
            decoder_dropout_p: float = 0.1,             # dropout probability of decoder network
            max_decoding_step: int = 1000,              # max decoding step
            stop_threshold: float = 0.5                 # stop threshold
    ) -> None:
        super(Decoder, self).__init__()
        self.num_mel_bins = num_mel_bins
        self.max_decoding_step = max_decoding_step
        self.decoder_lstm_dim = decoder_lstm_dim
        self.attn_lstm_dim = attn_lstm_dim
        self.embedding_dim = embedding_dim
        self.attn_dropout_p = attn_dropout_p
        self.decoder_dropout_p = decoder_dropout_p
        self.stop_threshold = stop_threshold

        self.prenet = PreNet(self.num_mel_bins, prenet_dim, prenet_dropout_p)
        self.lstm = nn.ModuleList([
            nn.LSTMCell(prenet_dim + embedding_dim, attn_lstm_dim, bias=True),
            nn.LSTMCell(attn_lstm_dim + embedding_dim, decoder_lstm_dim, bias=True)
        ])
        self.attention = LocationSensitiveAttention(
            lstm_hidden_dim=decoder_lstm_dim,
            embedding_dim=embedding_dim,
            attn_dim=attn_dim,
            location_conv_filter_size=location_conv_filter_size,
            location_conv_kernel_size=location_conv_kernel_size
        )
        self.mel_generator = Linear(decoder_lstm_dim + embedding_dim, num_mel_bins)
        self.stop_generator = Linear(decoder_lstm_dim + embedding_dim, 1)

    def _init_decoder_states(self, encoder_outputs: Tensor) -> Dict[str, Any]:
        lstm_outputs = list()
        lstm_hiddens = list()

        batch_size = encoder_outputs.size(0)
        seq_length = encoder_outputs.size(1)

        lstm_outputs.append(encoder_outputs.new_zeros(batch_size, self.attn_lstm_dim))
        lstm_outputs.append(encoder_outputs.new_zeros(batch_size, self.decoder_lstm_dim))

        lstm_hiddens.append(encoder_outputs.new_zeros(batch_size, self.attn_lstm_dim))
        lstm_hiddens.append(encoder_outputs.new_zeros(batch_size, self.decoder_lstm_dim))

        alignment = encoder_outputs.new_zeros(batch_size, seq_length)
        alignment_cum = encoder_outputs.new_zeros(batch_size, seq_length)
        context = encoder_outputs.new_zeros(batch_size, self.embedding_dim)

        return {
            "lstm_outputs": lstm_outputs,
            "lstm_hiddens": lstm_hiddens,
            "alignment": alignment,
            "alignment_cum": alignment_cum,
            "context": context
        }

    def parse_decoder_outputs(self, mel_outputs: list, stop_outputs: list, alignment: list) -> Dict[str, Tensor]:
        stop_outputs = torch.stack(stop_outputs).transpose(0, 1).contiguous()
        alignment = torch.stack(alignment).transpose(0, 1)

        mel_outputs = torch.stack(mel_outputs).transpose(0, 1).contiguous()
        mel_outputs = mel_outputs.view(mel_outputs.size(0), -1, self.num_mel_bins)
        mel_outputs = mel_outputs.transpose(1, 2)

        return {
            "mel_outputs": mel_outputs,
            "stop_outputs": stop_outputs,
            "alignments": alignment
        }

    def forward_step(
            self, input_var: Tensor,
            encoder_outputs: Tensor,
            lstm_outputs: list,
            lstm_hiddens: list,
            alignment: Tensor,
            alignment_cum: Tensor,
            context: Tensor
    ) -> Dict[str, Any]:
        input_var = input_var.squeeze(1)
        input_var = torch.cat((input_var, context), dim=-1)

        lstm_outputs[0], lstm_hiddens[0] = self.lstm[0](input_var, (lstm_outputs[0], lstm_hiddens[0]))
        lstm_outputs[0] = F.dropout(lstm_outputs[0], self.attn_dropout_p)

        concated_alignment = torch.cat((alignment.unsqueeze(1), alignment_cum.unsqueeze(1)), dim=1)
        context, alignment = self.attention(lstm_outputs[0], encoder_outputs, concated_alignment)
        alignment_cum += alignment

        input_var = torch.cat((lstm_outputs[0], context), dim=-1)

        lstm_outputs[1], lstm_hiddens[1] = self.lstm[1](input_var, (lstm_outputs[1], lstm_hiddens[1]))
        lstm_outputs[1] = F.dropout(lstm_outputs[1], p=self.decoder_dropout_p)

        output = torch.cat((lstm_hiddens[1], context), dim=-1)

        mel_output = self.mel_generator(output)
        stop_output = self.stop_generator(output)

        return {
            "mel_output": mel_output,
            "stop_output": stop_output,
            "alignment": alignment,
            "alignment_cum": alignment_cum,
            "context": context,
            "lstm_outputs": lstm_outputs,
            "lstm_hiddens": lstm_hiddens
        }

    def forward(
            self,
            encoder_outputs: Tensor,
            inputs: Optional[Tensor] = None,
            teacher_forcing_ratio: float = 1.0
    ) -> Dict[str, Tensor]:
        mel_outputs, stop_outputs, alignments = list(), list(), list()
        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        inputs, max_decoding_step = self.validate_args(encoder_outputs, inputs, teacher_forcing_ratio)
        decoder_states = self._init_decoder_states(encoder_outputs)

        if use_teacher_forcing:
            inputs = self.prenet(inputs)

            for di in range(max_decoding_step):
                input_var = inputs[:, di, :].unsqueeze(1)
                decoder_states = self.forward_step(
                    input_var=input_var,
                    encoder_outputs=encoder_outputs,
                    lstm_outputs=decoder_states["lstm_outputs"],
                    lstm_hiddens=decoder_states["lstm_hiddens"],
                    alignment=decoder_states["alignment"],
                    alignment_cum=decoder_states["alignment_cum"],
                    context=decoder_states["context"]
                )

                mel_outputs.append(decoder_states["mel_outputs"])
                stop_outputs.append(decoder_states["stop_output"])
                alignments.append(decoder_states["alignment"])

        else:
            input_var = inputs

            for di in range(max_decoding_step):
                input_var = self.prenet(input_var)
                decoder_states = self.forward_step(
                    input_var=input_var,
                    encoder_outputs=encoder_outputs,
                    lstm_outputs=decoder_states["lstm_outputs"],
                    lstm_hiddens=decoder_states["lstm_hiddens"],
                    alignment=decoder_states["alignment"],
                    alignment_cum=decoder_states["alignment_cum"],
                    context=decoder_states["context"]
                )

                mel_outputs.append(decoder_states["mel_output"])
                stop_outputs.append(decoder_states["stop_output"])
                alignments.append(decoder_states["alignment"])

                if torch.sigmoid(decoder_states["stop_output"]).item() > self.stop_threshold:
                    break

                input_var = decoder_states["mel_output"]

        return self.parse_decoder_outputs(mel_outputs, stop_outputs, alignments)

    def validate_args(
            self,
            encoder_outputs: Tensor,
            inputs: Optional[Any] = None,
            teacher_forcing_ratio: float = 1.0
    ) -> Tuple[Optional[Any], int]:
        assert encoder_outputs is not None

        batch_size = encoder_outputs.size(0)

        if input is None:  # inference
            inputs = encoder_outputs.new_zeros(batch_size, self.num_mel_bins)
            max_decoding_step = self.max_decoding_step

            if teacher_forcing_ratio > 0:
                raise ValueError("Teacher forcing has to be disabled (set 0) when no inputs is provided.")

        else:  # training
            go_frame = encoder_outputs.new_zeros(batch_size, self.num_mel_bins).unsqueeze(1)
            inputs = inputs.view(batch_size, int(inputs.size(1)), -1)

            inputs = torch.cat((go_frame, inputs), dim=1)

            max_decoding_step = inputs.size(1) - 1

        return inputs, max_decoding_step
