import datetime as dt
import math
import random

import torch

from matcha.models.components.vits_posterior import PosteriorEncoder

from matcha.utils.monotonic_align import maximum_path
from matcha import utils
from matcha.models.baselightningmodule import BaseLightningClass
from matcha.models.components.flow_matching import CFM
from matcha.models.components.text_encoder import TextEncoder
from matcha.utils.model import (
    denormalize,
    duration_loss,
    fix_len_compatibility,
    generate_path,
    sequence_mask,
)
from matcha.utils.audio import mel_spectrogram

from torch.nn import functional as F

from matcha.hifigan.models import Generator as HiFiGAN
from matcha.hifigan.config import v1
from matcha.hifigan.env import AttrDict
from matcha.hifigan.meldataset import mel_spectrogram
from matcha.models.components import commons
from matcha.utils.model import  normalize
log = utils.get_pylogger(__name__)


class MatchaTTS(BaseLightningClass):  # 🍵
    def __init__(
        self,
        n_vocab,
        n_spks,
        spk_emb_dim,
        n_feats,
        encoder,
        decoder,
        cfm,
        data_statistics,
        out_size,
        optimizer=None,
        scheduler=None,
    ):
        super().__init__()

        self.save_hyperparameters(logger=False)
        
        self.n_vocab = n_vocab
        self.n_spks = n_spks
        self.spk_emb_dim = spk_emb_dim
        self.n_feats = n_feats
        self.out_size = out_size

        # if n_spks > 1:
        #     self.spk_emb = torch.nn.Embedding(n_spks, spk_emb_dim)

        self.h = AttrDict(v1)
        self.hifigan = HiFiGAN(self.h)
        self.wav2mel = mel_spectrogram

        self.encoder = TextEncoder(
            encoder.encoder_type,
            encoder.encoder_params,
            encoder.duration_predictor_params,
            n_vocab,
            n_spks,
            spk_emb_dim,
        )
        self.enc_spec = PosteriorEncoder(
            n_feats,
            n_feats,
            n_feats,
            5,
            1,
            16,
            gin_channels=spk_emb_dim,
        )
        self.decoder = CFM(
            in_channels=2 * encoder.encoder_params.n_feats,
            out_channel=encoder.encoder_params.n_feats,
            cfm_params=cfm,
            decoder_params=decoder,
            n_spks=n_spks,
            spk_emb_dim=spk_emb_dim,
        )

        self.update_data_statistics(data_statistics)

    @torch.inference_mode()
    def synthesise(self, x, x_lengths, n_timesteps, temperature=1.0, spks=None, length_scale=1.0):
        """
        Generates mel-spectrogram from text. Returns:
            1. encoder outputs
            2. decoder outputs
            3. generated alignment

        Args:
            x (torch.Tensor): batch of texts, converted to a tensor with phoneme embedding ids.
                shape: (batch_size, max_text_length)
            x_lengths (torch.Tensor): lengths of texts in batch.
                shape: (batch_size,)
            n_timesteps (int): number of steps to use for reverse diffusion in decoder.
            temperature (float, optional): controls variance of terminal distribution.
            spks (bool, optional): speaker ids.
                shape: (batch_size,)
            length_scale (float, optional): controls speech pace.
                Increase value to slow down generated speech and vice versa.

        Returns:
            dict: {
                "encoder_outputs": torch.Tensor, shape: (batch_size, n_feats, max_mel_length),
                # Average mel spectrogram generated by the encoder
                "decoder_outputs": torch.Tensor, shape: (batch_size, n_feats, max_mel_length),
                # Refined mel spectrogram improved by the CFM
                "attn": torch.Tensor, shape: (batch_size, max_text_length, max_mel_length),
                # Alignment map between text and mel spectrogram
                "mel": torch.Tensor, shape: (batch_size, n_feats, max_mel_length),
                # Denormalized mel spectrogram
                "mel_lengths": torch.Tensor, shape: (batch_size,),
                # Lengths of mel spectrograms
                "rtf": float,
                # Real-time factor
        """
        # For RTF computation
        t = dt.datetime.now()

        # if self.n_spks > 1:
        #     # Get speaker embedding
        #     spks = self.spk_emb(spks.long())
        spks = spks.squeeze(2)
        # Get encoder_outputs `mu_x` and log-scaled token durations `logw`
        mu_x, logw, x_mask = self.encoder(x, x_lengths, spks)

        w = torch.exp(logw) * x_mask
        w_ceil = torch.ceil(w) * length_scale
        y_lengths = torch.clamp_min(torch.sum(w_ceil, [1, 2]), 1).long()
        y_max_length = y_lengths.max()
        y_max_length_ = fix_len_compatibility(y_max_length)

        # Using obtained durations `w` construct alignment map `attn`
        y_mask = sequence_mask(y_lengths, y_max_length_).unsqueeze(1).to(x_mask.dtype)
        attn_mask = x_mask.unsqueeze(-1) * y_mask.unsqueeze(2)
        attn = generate_path(w_ceil.squeeze(1), attn_mask.squeeze(1)).unsqueeze(1)

        # Align encoded text and get mu_y
        mu_y = torch.matmul(attn.squeeze(1).transpose(1, 2), mu_x.transpose(1, 2))
        mu_y = mu_y.transpose(1, 2)

        encoder_outputs = mu_y[:, :, :y_max_length_]

        # Generate sample tracing the probability flow
        decoder_outputs = self.decoder(encoder_outputs, y_mask, n_timesteps, temperature, spks)
        decoder_outputs = decoder_outputs[:, :, :y_max_length_]

        hifigan_out = self.hifigan(decoder_outputs)
        mel = self.wav2mel(hifigan_out.squeeze(1), num_mels=80, sampling_rate=22050, hop_size=256, win_size=1024, n_fft=1024, fmin=0, fmax=8000, center=False)
        # normalize mel
        
        mel = normalize(mel, self.mel_mean, self.mel_std)

        t = (dt.datetime.now() - t).total_seconds()
        rtf = t * 22050 / (decoder_outputs.shape[-1] * 256)

        return {
            "encoder_outputs": encoder_outputs,
            "decoder_outputs": mel,
            "attn": attn[:, :, :y_max_length],
            # "mel": mel
            "mel": denormalize(mel, self.mel_mean, self.mel_std),
            "mel_lengths": y_lengths,
            "rtf": rtf,
        }

    def forward(self, x, x_lengths, y, y_lengths, spks=None, out_size=None, cond=None):
        """
        Computes 3 losses:
            1. duration loss: loss between predicted token durations and those extracted by Monotinic Alignment Search (MAS).
            2. prior loss: loss between mel-spectrogram and encoder outputs.
            3. flow matching loss: loss between mel-spectrogram and decoder outputs.

        Args:
            x (torch.Tensor): batch of texts, converted to a tensor with phoneme embedding ids.
                shape: (batch_size, max_text_length)
            x_lengths (torch.Tensor): lengths of texts in batch.
                shape: (batch_size,)
            y (torch.Tensor): batch of corresponding mel-spectrograms.
                shape: (batch_size, n_feats, max_mel_length)
            y_lengths (torch.Tensor): lengths of mel-spectrograms in batch.
                shape: (batch_size,)
            out_size (int, optional): length (in mel's sampling rate) of segment to cut, on which decoder will be trained.
                Should be divisible by 2^{num of UNet downsamplings}. Needed to increase batch size.
            spks (torch.Tensor, optional): speaker ids.
                shape: (batch_size,)
        """
        # if self.n_spks > 1:
        #     # Get speaker embedding
        #     spks = self.spk_emb(spks)
        spks = spks.squeeze(2)
        # Get encoder_outputs `mu_x` and log-scaled token durations `logw`

        mu_x, logw, x_mask = self.encoder(x, x_lengths, spks)
        
        y_max_length = y.shape[-1]
        z_spec, spec_mask = self.enc_spec(y, y_lengths, g=spks.unsqueeze(1).transpose(1,2))
        z_spec = z_spec * spec_mask
        y_mask = sequence_mask(y_lengths, y_max_length).unsqueeze(1).to(x_mask)
        attn_mask = x_mask.unsqueeze(-1) * y_mask.unsqueeze(2)

        with torch.no_grad():
        # negative cross-entropy
            s_p_sq_r = torch.ones_like(mu_x) # [b, d, t]
            neg_cent1 = torch.sum(
                -0.5 * math.log(2 * math.pi)- torch.zeros_like(mu_x), [1], keepdim=True
            )
            # s_p_sq_r = torch.exp(-2 * log_x) # [b, d, t]
            # neg_cent1 = torch.sum(-0.5 * math.log(2 * math.pi) - log_x, [1], keepdim=True) # [b, 1, t_s]
      
            neg_cent2 = torch.einsum("bdt, bds -> bts", -0.5 * (z_spec**2), s_p_sq_r)
            neg_cent3 = torch.einsum("bdt, bds -> bts", z_spec, (mu_x * s_p_sq_r))
            neg_cent4 = torch.sum(
                -0.5 * (mu_x**2) * s_p_sq_r, [1], keepdim=True
            )  
            neg_cent = neg_cent1 + neg_cent2 + neg_cent3 + neg_cent4
            attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
            from matcha.utils.monotonic_align_vits import maximum_path
            attn = (
                maximum_path(neg_cent, attn_mask.squeeze(1)).unsqueeze(1).detach()
            )

        logw_ = torch.log(1e-8 + attn.sum(2)) * x_mask
        dur_loss = duration_loss(logw, logw_, x_lengths)
        attn = attn.squeeze(1).transpose(1,2)

        # Align encoded text with mel-spectrogram and get mu_y segment
        mu_y = torch.matmul(attn.squeeze(1).transpose(1, 2), mu_x.transpose(1, 2))
        mu_y = mu_y.transpose(1, 2)

        # Compute loss of the decoder

        diff_loss, _ = self.decoder.compute_loss(x1=z_spec, mask=y_mask, mu=mu_y.detach(), spks=spks, cond=cond)

        prior_loss = torch.sum(0.5 * ((z_spec - mu_y) ** 2 + math.log(2 * math.pi)) * y_mask)
        prior_loss = prior_loss / (torch.sum(y_mask) * self.n_feats)

        SEGMENT_SIZE = y_lengths.min()//2
        output_sliced, ids_slice = commons.rand_slice_segments(
                z_spec, y_lengths , segment_size=SEGMENT_SIZE
            )
        y_slice = commons.slice_segments(
                y, ids_slice, SEGMENT_SIZE)

        output_sliced_wav = self.hifigan(output_sliced)
        
        y_hat_mel = self.wav2mel(
                    output_sliced_wav.squeeze(1),
                    num_mels=80,
                    sampling_rate=22050,
                    hop_size=256,
                    win_size=1024,
                    n_fft=1024,
                    fmin=0,
                    fmax=8000,
                    center=False,
                    )

        # denorm_y = denormalize(y_slice, self.mel_mean, self.mel_std)
        y_hat_mel = normalize(y_hat_mel, self.mel_mean, self.mel_std)
        mel_loss = F.l1_loss(y_slice, y_hat_mel) * 45.0
        
        return dur_loss, prior_loss, diff_loss, mel_loss, y_hat_mel, y_slice
