import copy
import math
from typing import Optional, List


"""Lightning module for the tactic generator."""

import re
from subprocess import CalledProcessError

import torch
from lean_dojo.utils import execute
from loguru import logger

torch.set_float32_matmul_precision("medium")

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from models.end_to_end.tactic_models.dpo.model import GenTacModel

select_batch_idxs = lambda x, idxs: torch.gather(x, dim=0, index=idxs.repeat(*x.shape[1:], 1).permute(len(x.shape) - 1,
                                                                                                      *list(range(
                                                                                                          len(x.shape) - 1))))
map_all_kvs = lambda f, kvs: tuple([tuple(map(f, items)) for items in kvs])

map_decoder_kvs = lambda f, kvs: tuple([tuple(map(f, items[:2])) + tuple(items[2:]) for items in kvs])

pad_sequence = lambda seq, to_len, val, device, dim: torch.cat(
    (seq, torch.full((*seq.shape[:dim], to_len - seq.shape[dim], *seq.shape[(dim + 1):]), val).to(device)), dim=dim)


def update_kvs(kvs, updated_kvs, lens_chosen, idx):
    for i, layer in enumerate(kvs):
        for x, item in enumerate(layer):
            item[lens_chosen, :, idx, :] = updated_kvs[i][x][:, :, idx, :]
    return kvs


def top_k_logits(logits, k):
    # logits = (batch, time, dim)
    _, bottom_k_idx = torch.topk(-logits, logits.shape[2] - k, dim=2)
    return torch.scatter(logits, dim=2, index=bottom_k_idx, value=float('-inf'))


def top_p_logits(logits, p):
    # logits = (batch, time, dim)
    sorted_logits, _ = torch.sort(logits, dim=2, descending=True)
    num_to_take = torch.sum(torch.cumsum(F.softmax(sorted_logits, dim=2), dim=2) <= p, dim=2).unsqueeze(2)
    mask = logits < torch.gather(sorted_logits, dim=2, index=torch.clamp(num_to_take, max=logits.shape[2] - 1))
    return logits.masked_fill(mask, float('-inf'))


def process_logits(logits, temp=1.0, top_k=None, top_p=None):
    logits /= temp
    if top_k is not None:
        logits = top_k_logits(logits, top_k)
    if top_p is not None:
        logits = top_p_logits(logits, top_p)
    return logits


def always_terminate(s: np.ndarray):
    return True


def parameter_norm(model: nn.Module):
    norm = 0.0
    for param in model.parameters():
        norm += (param.norm() ** 2).item()
    return math.sqrt(norm)


def get_transformer_logs(attentions: List[torch.Tensor], model: nn.Module, attn_mask: torch.Tensor):
    logs = {}
    n = attn_mask.sum()

    model_attention_entropy = -sum(
        map(lambda x: ((x * torch.log(x + 1e-7)).sum(dim=-1) * attn_mask.unsqueeze(1)).sum().item(), attentions)) / (
                                      len(attentions) * n)
    model_parameter_norm = parameter_norm(model)

    logs['attention_entropy'] = (model_attention_entropy, n * len(attentions))
    logs['parameter_norm'] = (model_parameter_norm, 1)

    return logs


class TransformerMLP(nn.Module):
    def __init__(self, emb_dim, h_dim, out_dim, dropout):
        super().__init__()
        self.emb_dim = emb_dim
        self.h_dim = h_dim
        self.dropout = dropout
        self.ff1 = nn.Linear(emb_dim, h_dim)
        self.ff2 = nn.Linear(h_dim, emb_dim)
        self.ln1 = nn.LayerNorm(emb_dim)
        self.ln2 = nn.LayerNorm(emb_dim)
        self.output_layer = nn.Linear(emb_dim, out_dim)

    def forward(self, x):
        return self.output_layer(
            self.ln2(x + F.dropout(self.ff2(F.gelu(self.ff1(self.ln1(x)))), p=self.dropout, training=self.training)))


class PerTokenIQL(GenTacModel):
    def __init__(self, config):
        super().__init__(config)

        self.save_hyperparameters()

        self.h_dim = self.generator.config.d_model

        self.alpha = config.alpha
        self.gamma = config.gamma
        self.beta = config.beta
        self.transition_weight = config.transition_weight
        self.clip_weight = config.clip_weight
        self.value_max = config.value_max
        self.value_min = config.value_min
        self.detach_v = config.detach_v
        self.detach_pi = config.detach_pi
        self.detach_q = config.detach_q
        self.double_q = config.double_q
        self.tau = config.tau
        self.separate_policy = config.separate_policy
        self.separate_target = config.separate_target
        self.exp_weights = config.exp_weights
        self.advanced_mlp = config.advanced_mlp
        self.cql_temp = config.cql_temp

        if not self.advanced_mlp:
            self.v = nn.Sequential(
                nn.Linear(self.h_dim, self.h_dim * 2),
                nn.ReLU(),
                nn.Linear(self.h_dim * 2, 1),
            )
        else:
            self.v = TransformerMLP(self.h_dim,
                                    4 * self.h_dim if self.generator.config.n_inner is None else self.generator.config.n_inner,
                                    1, self.generator.config.resid_pdrop)
        if not self.advanced_mlp:
            self.q = nn.Sequential(
                nn.Linear(self.h_dim, self.h_dim * 2),
                nn.ReLU(),
                nn.Linear(self.h_dim * 2, len(self.tokenizer)),
            )
        else:
            self.q = TransformerMLP(self.h_dim,
                                    4 * self.h_dim if self.generator.config.n_inner is None else self.generator.config.n_inner,
                                    len(self.tokenizer), self.generator.config.resid_pdrop)
        if self.double_q:
            if not self.advanced_mlp:
                self.q2 = nn.Sequential(
                    nn.Linear(self.h_dim, self.h_dim * 2),
                    nn.ReLU(),
                    nn.Linear(self.h_dim * 2, len(self.tokenizer)),
                )
            else:
                self.q2 = TransformerMLP(self.h_dim,
                                         4 * self.h_dim if self.generator.config.n_inner is None else self.generator.config.n_inner,
                                         len(self.tokenizer),
                                         self.generator.config.resid_pdrop)
        if not self.advanced_mlp:
            self.target_q = nn.Sequential(
                nn.Linear(self.h_dim, self.h_dim * 2),
                nn.ReLU(),
                nn.Linear(self.h_dim * 2, len(self.tokenizer)),
            )
        else:
            self.target_q = TransformerMLP(self.h_dim,
                                           4 * self.h_dim if self.generator.config.n_inner is None else self.generator.config.n_inner,
                                           len(self.tokenizer),
                                           self.generator.config.resid_pdrop)
        if self.double_q:
            if not self.advanced_mlp:
                self.target_q2 = nn.Sequential(
                    nn.Linear(self.h_dim, self.h_dim * 2),
                    nn.ReLU(),
                    nn.Linear(self.h_dim * 2, len(self.tokenizer)),
                )
            else:
                self.target_q2 = TransformerMLP(self.h_dim,
                                                4 * self.h_dim if self.generator.config.n_inner is None else self.generator.config.n_inner,
                                                len(self.tokenizer),
                                                self.generator.config.resid_pdrop)

        for target_param, local_param in zip(self.target_q.parameters(), self.q.parameters()):
            target_param.data.copy_(local_param.data)

        if self.double_q:
            for target_param, local_param in zip(self.target_q2.parameters(), self.q2.parameters()):
                target_param.data.copy_(local_param.data)

        if self.separate_target:
            self.lm_target = copy.deepcopy(self.generator)
        else:
            self.lm_target = None

        if self.separate_policy:
            self.lm_policy = copy.deepcopy(self.generator)
        else:
            self.lm_policy = None

        if self.lm_policy is None:
            self.pi = self.generator.lm_head
        else:
            self.pi = self.lm_policy.lm_head

    def clip_values(self, values):
        if self.value_min is not None or self.value_max is not None:
            return torch.clip(values, self.value_min, self.value_max)
        return values

    def training_step(self, batch, batch_idx: int):
        loss, (v_loss, q_loss, cql_loss, token_loss) = self.get_loss(batch)

        self.log_dict({'train_v_loss': v_loss,
                       'train_q_loss': q_loss,
                       'train_cql_loss': cql_loss,
                       'train_token_loss': token_loss},
                      on_step=True,
                      on_epoch=True,
                      sync_dist=True,
                      batch_size=len(batch),
                      prog_bar=True)

        self.log(
            "loss_train",
            loss,
            on_step=True,
            on_epoch=True,
            sync_dist=True,
            batch_size=len(batch),
            prog_bar=True
        )

        self.log(
            "loss_train",
            loss,
            on_step=True,
            on_epoch=True,
            sync_dist=True,
            batch_size=len(batch),
            prog_bar=True
        )

        return loss

    def validation_step(self, batch, batch_idx: int):
        loss, (v_loss, q_loss, cql_loss, token_loss) = self.get_loss(batch)
        self.log(
            "loss_val",
            loss,
            on_step=True,
            on_epoch=True,
            sync_dist=True,
            batch_size=len(batch),
            prog_bar=True
        )

        self.log(
            "loss_val",
            loss,
            on_step=True,
            on_epoch=True,
            sync_dist=True,
            batch_size=len(batch),
            prog_bar=True
        )

        self.log_dict({'val_v_loss': v_loss,
                       'val_q_loss': q_loss,
                       'val_cql_loss': cql_loss,
                       'val_token_loss': token_loss},
                      on_step=True,
                      on_epoch=True,
                      sync_dist=True,
                      batch_size=len(batch),
                      prog_bar=True)

        return loss

    def inference_forward(self,
                          input_ids: torch.Tensor,
                          input_attn_mask: Optional[torch.Tensor] = None,
                          target_ids: Optional[torch.Tensor] = None,
                          target_attn_mask: Optional[torch.Tensor] = None,
                          qv_kwargs=None, policy_kwargs=None, target_kwargs=None,
                          skip_policy_on_train=False,
                          detach_full_policy=False
                          ):

        if qv_kwargs is None:
            qv_kwargs = {}
        if target_kwargs is None:
            target_kwargs = {}
        if policy_kwargs is None:
            policy_kwargs = {}

        if self.lm_target is None:
            qv_kwargs.update(target_kwargs)
        if self.lm_policy is None:
            qv_kwargs.update(policy_kwargs)

        if input_attn_mask is None:
            input_attn_mask = torch.ones(input_ids.shape, dtype=torch.long).to(self.device)

        if target_ids is None:
            # target_ids = torch.empty((input_ids.shape[0], 0, self.h_dim), dtype=torch.long).to(self.device)

            # set target_ids as padding index (start of sequence id)
            target_ids = torch.ones((input_ids.shape[0], 1), dtype=torch.long).to(
                self.device) * self.tokenizer.pad_token_id

        if target_attn_mask is None:
            # target_attn_mask = torch.ones(target_ids.shape[:2], dtype=torch.long).to(self.device)
            target_attn_mask = torch.ones(target_ids.shape, dtype=torch.long).to(self.device)

        input_attn_mask = input_attn_mask

        model_outputs = self.generator(input_ids=input_ids,
                                       attention_mask=input_attn_mask,
                                       decoder_input_ids=target_ids,
                                       decoder_attention_mask=target_attn_mask,
                                       output_hidden_states=True,
                                       **qv_kwargs)

        all_model_outputs = {
            'qv_model_outputs': model_outputs,
            'policy_model_outputs': model_outputs,
            'target_model_outputs': model_outputs
        }

        if self.advanced_mlp:
            hidden_states = model_outputs.decoder_hidden_states[-2]
        else:
            hidden_states = model_outputs.decoder_hidden_states[-1]
        if self.lm_target is None:
            target_hidden_states = hidden_states
        else:
            with torch.no_grad():
                target_outputs = self.generator(input_ids=input_ids,
                                                attention_mask=input_attn_mask,
                                                decoder_input_ids=target_ids,
                                                decoder_attention_mask=target_attn_mask,
                                                output_hidden_states=True,
                                                **qv_kwargs)

            all_model_outputs['target_model_outputs'] = target_outputs

            if self.advanced_mlp:
                target_hidden_states = target_outputs.decoder_hidden_states[-2]
            else:
                target_hidden_states = target_outputs.decoder_hidden_states[-1]

        if self.lm_policy is None:
            policy_hidden_states = model_outputs.decoder_hidden_states[-1]
        else:
            if skip_policy_on_train and self.training:
                policy_hidden_states = hidden_states
            else:
                if detach_full_policy:
                    with torch.no_grad():
                        policy_outputs = self.generator(input_ids=input_ids,
                                                        attention_mask=input_attn_mask,
                                                        decoder_input_ids=target_ids,
                                                        decoder_attention_mask=target_attn_mask,
                                                        output_hidden_states=True,
                                                        **qv_kwargs)
                else:
                    policy_outputs = self.generator(input_ids=input_ids,
                                                    attention_mask=input_attn_mask,
                                                    decoder_input_ids=target_ids,
                                                    decoder_attention_mask=target_attn_mask,
                                                    output_hidden_states=True,
                                                    **qv_kwargs)

                all_model_outputs['policy_model_outputs'] = policy_outputs

                policy_hidden_states = policy_outputs.decoder_hidden_states[-1]

        state_hidden_states = hidden_states

        # for inference, keep the last q value
        action_hidden_states = hidden_states  # [:, :-1, :]

        action_target_hidden_states = target_hidden_states  # [:, :-1, :]

        vs = self.v(state_hidden_states.detach() if self.detach_v else state_hidden_states).squeeze(2)

        qs = self.q(action_hidden_states.detach() if self.detach_q else action_hidden_states)

        if self.double_q:
            qs2 = self.q2(action_hidden_states.detach() if self.detach_q else action_hidden_states)

        with torch.no_grad():
            target_qs = self.target_q(action_target_hidden_states)
            if self.double_q:
                target_qs2 = self.target_q2(action_target_hidden_states)

        if skip_policy_on_train and self.training and self.lm_policy is not None:
            logits = torch.zeros((policy_hidden_states.shape[0], policy_hidden_states.shape[1],
                                  len(self.tokenizer),)).to(self.device)
        else:
            if detach_full_policy:
                with torch.no_grad():
                    logits = self.pi(policy_hidden_states.detach() if self.detach_pi else policy_hidden_states)
            else:
                logits = self.pi(policy_hidden_states.detach() if self.detach_pi else policy_hidden_states)

        return {
            'model_outputs': all_model_outputs,
            'vs': vs,
            'target_vs': vs,
            'qs': (qs, qs2,) if self.double_q else qs,
            'target_qs': self.clip_values(torch.minimum(target_qs, target_qs2) if self.double_q else target_qs),
            'logits': logits,
        }

    def forward(self,
                input_ids: torch.Tensor,
                input_attn_mask,
                target_ids,
                target_attn_mask,
                qv_kwargs=None, policy_kwargs=None, target_kwargs=None,
                skip_policy_on_train=False,
                detach_full_policy=False
                ):

        if qv_kwargs is None:
            qv_kwargs = {}
        if target_kwargs is None:
            target_kwargs = {}
        if policy_kwargs is None:
            policy_kwargs = {}

        if self.lm_target is None:
            qv_kwargs.update(target_kwargs)
        if self.lm_policy is None:
            qv_kwargs.update(policy_kwargs)

        model_outputs = self.generator(input_ids=input_ids,
                                       attention_mask=input_attn_mask,
                                       decoder_input_ids=target_ids,
                                       decoder_attention_mask=target_attn_mask,
                                       output_hidden_states=True,
                                       **qv_kwargs)

        all_model_outputs = {
            'qv_model_outputs': model_outputs,
            'policy_model_outputs': model_outputs,
            'target_model_outputs': model_outputs
        }

        if self.advanced_mlp:
            hidden_states = model_outputs.decoder_hidden_states[-2]
        else:
            hidden_states = model_outputs.decoder_hidden_states[-1]
        if self.lm_target is None:
            target_hidden_states = hidden_states
        else:
            with torch.no_grad():
                target_outputs = self.generator(input_ids=input_ids,
                                                attention_mask=input_attn_mask,
                                                decoder_input_ids=target_ids,
                                                decoder_attention_mask=target_attn_mask,
                                                output_hidden_states=True,
                                                **qv_kwargs)

            all_model_outputs['target_model_outputs'] = target_outputs

            if self.advanced_mlp:
                target_hidden_states = target_outputs.decoder_hidden_states[-2]
            else:
                target_hidden_states = target_outputs.decoder_hidden_states[-1]

        if self.lm_policy is None:
            policy_hidden_states = model_outputs.decoder_hidden_states[-1]
        else:
            if skip_policy_on_train and self.training:
                policy_hidden_states = hidden_states
            else:
                if detach_full_policy:
                    with torch.no_grad():
                        policy_outputs = self.generator(input_ids=input_ids,
                                                        attention_mask=input_attn_mask,
                                                        decoder_input_ids=target_ids,
                                                        decoder_attention_mask=target_attn_mask,
                                                        output_hidden_states=True,
                                                        **qv_kwargs)
                else:
                    policy_outputs = self.generator(input_ids=input_ids,
                                                    attention_mask=input_attn_mask,
                                                    decoder_input_ids=target_ids,
                                                    decoder_attention_mask=target_attn_mask,
                                                    output_hidden_states=True,
                                                    **qv_kwargs)

                all_model_outputs['policy_model_outputs'] = policy_outputs

                policy_hidden_states = policy_outputs.decoder_hidden_states[-1]

        state_hidden_states = hidden_states

        # action hidden_states will just ignore the last token (as no valid Q value)
        action_hidden_states = hidden_states[:, :-1, :]

        action_target_hidden_states = target_hidden_states[:, :-1, :]

        vs = self.v(state_hidden_states.detach() if self.detach_v else state_hidden_states).squeeze(2)

        qs = self.q(action_hidden_states.detach() if self.detach_q else action_hidden_states)

        if self.double_q:
            qs2 = self.q2(action_hidden_states.detach() if self.detach_q else action_hidden_states)

        with torch.no_grad():
            target_qs = self.target_q(action_target_hidden_states)
            if self.double_q:
                target_qs2 = self.target_q2(action_target_hidden_states)

        if skip_policy_on_train and self.training and self.lm_policy is not None:
            logits = torch.zeros((policy_hidden_states.shape[0], policy_hidden_states.shape[1],
                                  len(self.tokenizer),)).to(self.device)
        else:
            if detach_full_policy:
                with torch.no_grad():
                    logits = self.pi(policy_hidden_states.detach() if self.detach_pi else policy_hidden_states)
            else:
                logits = self.pi(policy_hidden_states.detach() if self.detach_pi else policy_hidden_states)

        return {
            'model_outputs': all_model_outputs,
            'vs': vs,
            'target_vs': vs,
            'qs': (qs, qs2,) if self.double_q else qs,
            'target_qs': self.clip_values(torch.minimum(target_qs, target_qs2) if self.double_q else target_qs),
            'logits': logits,
        }

    def get_downstream_rs(self, rs, gamma):
        gamma_row = torch.cumprod(torch.full(rs.shape, gamma).to(self.device), dim=1)
        gamma_tensor = torch.triu(gamma_row.unsqueeze(1) / gamma_row.unsqueeze(2))
        return (gamma_tensor * rs.unsqueeze(1)).sum(dim=2)

    # retrieve token weightings from advantage estimates, with self.transition_weight for non-action tokens
    def get_weights(self,
                    tokens: torch.Tensor,
                    vs: torch.Tensor,
                    qs: torch.Tensor,
                    action_ids: torch.Tensor
                    ):

        if self.exp_weights:
            w_values = torch.exp(self.beta * (qs - vs))
        else:
            adv_sign = ((qs - vs) > 0.0).float()
            w_values = self.beta * adv_sign + (1 - self.beta) * (1 - adv_sign)

        weights = torch.full(tokens.shape, self.transition_weight, dtype=w_values.dtype).to(self.device)

        if action_ids.shape[1] == 0:
            n = torch.zeros((tokens.shape[0],)).long().to(self.device)
        else:
            n = torch.argmax(action_ids, dim=1) + 1

        for i in range(tokens.shape[0]):
            # updates weights[i] at inds action_ids[i, :n[i]] with w_values[i, :n[i]]
            # keeping non action_ids as transition_weight
            weights[i] = torch.scatter(weights[i], dim=0, index=action_ids[i, :n[i]], src=w_values[i, :n[i]])

        if self.clip_weight is not None:
            weights = torch.clip(weights, max=self.clip_weight)

        return weights

    # Advantage Weighted Actor Critic (AWAC, https://arxiv.org/pdf/2006.09359.pdf)
    # Used to train the behavioural model \pi_{\beta}
    def awac_loss(self, tokens, attn_mask, logits, w):
        w = w.detach()

        losses = F.cross_entropy(logits[:, :-1, :].reshape(-1, logits.shape[-1]), tokens[:, 1:].reshape(-1),
                                 reduction='none')

        losses = losses.reshape(tokens.shape[0], tokens.shape[1] - 1)

        return (losses * w[:, :-1] * attn_mask[:, 1:]).sum() / attn_mask[:, 1:].sum()

    def get_v_loss(self, vs, target_qs, terminals):
        target_qs = target_qs.detach()
        return (((target_qs >= vs).int() * self.tau * (target_qs - vs) ** 2 + (target_qs < vs).int() * (
                1 - self.tau) * (target_qs - vs) ** 2) * (1 - terminals[:, :-1])).sum() / max(
            (1 - terminals[:, :-1]).sum().item(), 1.0)

    def get_q_loss(self, vns, qs, rs, gamma, terminals):
        vns = vns.detach()
        if self.double_q:
            q1, q2 = qs
            l1 = ((((1 - terminals[:, 1:]) * vns * gamma + rs - q1) ** 2) * (1 - terminals[:, :-1])).sum() / max(
                (1 - terminals[:, :-1]).sum().item(), 1.0)
            l2 = ((((1 - terminals[:, 1:]) * vns * gamma + rs - q2) ** 2) * (1 - terminals[:, :-1])).sum() / max(
                (1 - terminals[:, :-1]).sum().item(), 1.0)
            return l1 + l2
        return ((((1 - terminals[:, 1:]) * vns * gamma + rs - qs) ** 2) * (1 - terminals[:, :-1])).sum() / max(
            (1 - terminals[:, :-1]).sum().item(), 1.0)

    def get_cql_loss(self, qs, action_tokens, terminals):
        n = (1 - terminals[:, :-1]).sum()
        if self.double_q:
            q1, q2 = qs
            b, t, d = q1.shape
            return ((F.cross_entropy(q1.reshape(-1, d) / self.cql_temp, action_tokens.reshape(-1),
                                     reduction='none').reshape(b, t) * (1 - terminals[:, :-1])) + (
                            F.cross_entropy(q2.reshape(-1, d) / self.cql_temp, action_tokens.reshape(-1),
                                            reduction='none').reshape(b, t) * (1 - terminals[:, :-1]))).sum() / max(
                n.item(), 1.0)
        b, t, d = qs.shape
        return (F.cross_entropy(qs.reshape(-1, d) / self.cql_temp, action_tokens.reshape(-1), reduction='none').reshape(
            b, t) * (1 - terminals[:, :-1])).sum() / max(n.item(), 1.0)

    def get_qvs(self, items,
                qv_kwargs=None, policy_kwargs=None, target_kwargs=None,
                **kwargs):

        prepared_inputs = items

        tokens, attn_mask = prepared_inputs['tokens'], prepared_inputs['attn_mask']
        target, target_mask = prepared_inputs['target'], prepared_inputs['target_mask']

        rs = prepared_inputs['rewards']

        self_outputs = self.forward(tokens, attn_mask, target, target_mask,
                                    qv_kwargs, policy_kwargs, target_kwargs,
                                    **kwargs)

        model_outputs, vs, qs = self_outputs['model_outputs'], self_outputs['vs'], self_outputs['qs']

        target_qs, logits = self_outputs['target_qs'], self_outputs['logits']

        # values for the V updates (ignoring last)
        vt = vs[:, :-1]
        # values for the Q updates (ignoring first)
        vtp1 = vs[:, 1:]

        # set action_ids as the non-masked target indices
        b, t = vt.shape

        action_ids = torch.arange(0, t, device=self.device).repeat(b, 1)

        action_ids = action_ids * target_mask[:, :-1]

        select_tokens = torch.gather(target[:, 1:], dim=1, index=action_ids)

        # want terminals to be 0 for sequence length in each sample, then 1 for remainder
        # hence it is the opposite of attn_mask
        terminals = torch.abs(1 - target_mask)

        cql_term = self.get_cql_loss(qs, select_tokens, terminals)

        if self.double_q:
            q1, q2 = qs
            q1 = torch.gather(q1, dim=2, index=select_tokens.unsqueeze(2)).squeeze(2)
            q2 = torch.gather(q2, dim=2, index=select_tokens.unsqueeze(2)).squeeze(2)
            qs = (q1, q2,)
            # tok_seq = [self.tokenizer.decode([token]) for token in select_tokens[0].detach().cpu().tolist()]
            # max_q_seq = torch.max(q1, q2)[0, :].detach().cpu().tolist()
            # print(self.tokenizer.decode(tokens[0, :][:attn_mask[0, :].sum().long()].tolist(),
            #                             clean_up_tokenization_spaces=False))
            # print(list(zip(tok_seq, max_q_seq)))
            # print(rs)
        else:
            qs = torch.gather(qs, dim=2, index=select_tokens.unsqueeze(2)).squeeze(2)

        target_qs = torch.gather(target_qs, dim=2, index=select_tokens.unsqueeze(2)).squeeze(2)

        with torch.no_grad():
            weights = self.get_weights(target, vt, target_qs, action_ids)

        return {
            # tokens used are the targets
            'tokens': target,
            'attn_mask': target_mask,
            'model_outputs': model_outputs,
            'vs': vt,
            'qs': qs,
            'vns': vtp1,
            'target_vs': vt,
            'target_qs': target_qs,
            'target_vns': vtp1,
            'rs': rs,
            'logits': logits,
            'weights': weights,
            'cql_term': cql_term,
            'terminals': terminals
        }

    def get_loss(self,
                 items,
                 awac_weight=1.0,
                 v_loss_weight=1.0,
                 q_loss_weight=1.0,
                 cql_loss_weight=1e-4,
                 mc_returns=False):

        get_qvs_outputs = self.get_qvs(items,
                                       qv_kwargs={'output_attentions': True},
                                       policy_kwargs={'output_attentions': True},
                                       target_kwargs={'output_attentions': True},
                                       skip_policy_on_train=(awac_weight == 0.0),
                                       )

        tokens, attn_mask, model_outputs = get_qvs_outputs['tokens'], get_qvs_outputs['attn_mask'], get_qvs_outputs[
            'model_outputs']

        vs, qs = get_qvs_outputs['vs'], get_qvs_outputs['qs']

        vns, target_qs, rs = get_qvs_outputs['vns'], get_qvs_outputs['target_qs'], get_qvs_outputs['rs']

        logits, weights = get_qvs_outputs['logits'], get_qvs_outputs['weights']

        terminals = get_qvs_outputs['terminals']

        rs_downstream = self.get_downstream_rs(rs, self.gamma)

        if mc_returns:
            v_loss = self.get_v_loss(vs, rs_downstream, terminals)
        else:
            v_loss = self.get_v_loss(vs, target_qs, terminals)

        q_loss = self.get_q_loss(vns, qs, rs, self.gamma, terminals)

        cql_loss = get_qvs_outputs['cql_term']

        token_loss = self.awac_loss(tokens, attn_mask, logits, weights)

        loss = awac_weight * token_loss + v_loss_weight * v_loss + q_loss_weight * q_loss + cql_loss_weight * cql_loss

        return loss, (v_loss, q_loss, cql_loss, token_loss)


    def run_eval(self) -> None:
        ckpt_path = f"{self.trainer.log_dir}/checkpoints/last_eval.ckpt"
        self.trainer.save_checkpoint(ckpt_path)
        logger.info(f"Saved checkpoint to {ckpt_path}")

        # todo get config file from config
        cmd = f"python -m experiments.end_to_end.end_to_end_experiment --config-name=end_to_end/leandojo num_theorems={self.eval_config.eval_num_theorems}" \
              f" shuffle={self.eval_config.shuffle} env_timeout={self.eval_config.timeout} tac_model.ckpt_path={ckpt_path} log_level='ERROR' tac_model.model='ilql'" \
              f" exp_config.name=eval_epoch_{self.trainer.current_epoch} exp_config.experiment=seq2seq_eval"

        logger.info(f'Running evaluation with {cmd}')

        try:
            # todo better/live output?:
            #  https://stackoverflow.com/questions/4417546/constantly-print-subprocess-output-while-process-is-running
            _, err = execute(cmd, capture_output=True)
        except CalledProcessError as ex:
            logger.error(ex)
            logger.error("Failed to evaluate.")
            return

        m = re.search(r"Pass@1: (\S+)", err)
        assert m is not None, err
        acc = float(m.group(1))
        self.log("Pass@1_val", acc, prog_bar=True)
        logger.info(f"Pass@1: {acc}")


    ##############
    # Prediction #
    ##############

    def beamsearch_gen(self, state, state_ids, state_mask, num_samples,
                       temp=1.0, top_k=None, top_p=None, exp_adv=True,
                       adv_weight=1.0, adv_clip=None,
                       include_logits=True, include_adv=True):

        tokenizer = self.tokenizer
        max_length = self.max_seq_len

        device = self.device

        bsize, vocab_size = len(state), len(tokenizer)

        n = bsize * num_samples

        max_generation_len = max_length + 1

        input_strs = state

        # length of the input sequences
        original_dialogue_lens = state_mask.sum(dim=1)

        batch_indicator = torch.stack(num_samples * [torch.arange(0, bsize).to(device)], dim=1)

        # length of each generated sequence repeated [l1 * (num_samples), l2 * (num_samples),...]
        dialogue_lens = torch.repeat_interleave(original_dialogue_lens, num_samples, dim=0)

        curr_scores = torch.zeros(bsize, num_samples).to(device)  # (batch, k)
        logit_scores = torch.zeros(bsize, num_samples).to(device)  # (batch, k)
        termination_mask = torch.full((n,), 1).to(device)

        t = 1

        base_logits = torch.full((dialogue_lens.shape[0],), 0.0).to(device)

        batched_inputs = torch.repeat_interleave(state_ids, num_samples, dim=0)
        batched_mask = torch.repeat_interleave(state_mask, num_samples, dim=0)

        # first pass of the model before caching

        target_ids = torch.ones((state_ids.shape[0], 1), dtype=torch.long).to(
            self.device) * self.tokenizer.pad_token_id

        tokens = pad_sequence(torch.repeat_interleave(target_ids, num_samples, dim=0), max_length,
                              tokenizer.pad_token_id,
                              device, 1)

        while termination_mask.sum() > 0 and t < max_length:
            curr_token = tokens[:, t - 1].unsqueeze(1)

            if t == 1:
                # since we are using past_key_values, only need to provide a single token at a time
                # as we only want the Q/V values for the one token, state/action idxs gives us this
                iql_outputs = self.inference_forward(batched_inputs,
                                                     batched_mask,
                                                     target_ids=curr_token,
                                                     qv_kwargs={'use_cache': True},
                                                     policy_kwargs={'use_cache': True},
                                                     target_kwargs={'use_cache': True}, )

                model_outputs = iql_outputs['model_outputs']

                kvs = {'qv': model_outputs['qv_model_outputs'].past_key_values}

                if self.lm_target is not None:
                    kvs['target'] = model_outputs['target_model_outputs'].past_key_values
                if self.lm_policy is not None:
                    kvs['policy'] = model_outputs['policy_model_outputs'].past_key_values

            else:
                curr_kvs = kvs['qv']
                curr_target_kvs = kvs['target']
                curr_policy_kvs = kvs['policy']

                # since we are using past_key_values, only need to provide a single token at a time
                # as we only want the Q/V values for the one token, state/action idxs gives us this
                iql_outputs = self.inference_forward(batched_inputs,
                                                     batched_mask,
                                                     target_ids=curr_token,
                                                     qv_kwargs={'use_cache': True, 'past_key_values': curr_kvs},
                                                     policy_kwargs={'use_cache': True,
                                                                    'past_key_values': curr_policy_kvs},
                                                     target_kwargs={'use_cache': True,
                                                                    'past_key_values': curr_target_kvs})

            model_outputs, logits = iql_outputs['model_outputs'], iql_outputs['logits']

            logits[:, 0, tokenizer.pad_token_id] = torch.where(termination_mask == 1, float('-inf'), 1e7)

            edited_logits = process_logits(logits.clone(), temp=temp, top_k=top_k, top_p=top_p)

            vs, qs = iql_outputs['target_vs'], iql_outputs['target_qs']

            if exp_adv:
                adv_logits = adv_weight * (qs - vs.unsqueeze(2))
            else:
                adv_sign = ((qs - vs.unsqueeze(2)) > 0.0).float()
                adv_logits = adv_weight * adv_sign + (1 - adv_weight) * (1 - adv_sign)
                adv_logits = torch.log(adv_logits)
            if adv_clip is not None:
                adv_logits = torch.clip(adv_logits, max=adv_clip)

            adv_logits[:, 0, tokenizer.pad_token_id] = torch.where(termination_mask == 1, float('-inf'), 1e7)

            full_logits = (edited_logits if include_logits else 0.0) + (
                adv_logits if include_adv else 0.0) + base_logits.unsqueeze(1).unsqueeze(
                2)  # (batch*k, (time?), vocab_size)

            scores = (torch.log(F.softmax(full_logits, dim=-1)).reshape(1, bsize, num_samples, -1).permute(3, 0, 1, 2)
                      + curr_scores).permute(1, 2, 3, 0).reshape(1, bsize, -1)  # (time, batch, k*vocab)

            # mask out all tokens except for the first beam (only when t is the end of the original sequence)
            if t == 1:
                scores[0, :, vocab_size:] = float('-inf')

            curr_scores, top_k_ = torch.topk(scores[0, :, :], k=num_samples, dim=1)  # (batch, k), (batch, k)

            # top_k_ is indices, indexed by multiple of vocab_size for each beam
            tokens = tokens[(batch_indicator * num_samples + (top_k_ // vocab_size)).reshape(-1), :]

            logits = logits[(batch_indicator * num_samples + (top_k_ // vocab_size)).reshape(-1), :, :]

            # total score for sequence
            logit_scores += torch.gather(torch.log(F.softmax(logits, dim=-1)).squeeze(1), dim=1,
                                         index=(top_k_.reshape(-1) % vocab_size).unsqueeze(1)).squeeze(1).reshape(-1,
                                                                                                                  num_samples)
            tokens[:, t] = top_k_.reshape(-1) % vocab_size  # (batch*k,)

            # logits: (batch * k, 1, vocab_size), tokens: (batch*k, max_seq_len),

            kvs['qv'] = model_outputs['qv_model_outputs'].past_key_values

            if 'target' in kvs:
                kvs['target'] = model_outputs['target_model_outputs'].past_key_values

            if 'policy' in kvs:
                kvs['policy'] = model_outputs['policy_model_outputs'].past_key_values

            termination_mask = termination_mask[(batch_indicator * num_samples + (top_k_ // vocab_size)).reshape(-1)]

            for idx in range(n):
                if tokens[idx, t] == tokenizer.eos_token_id:
                    termination_mask[idx] *= 0

            t += 1

            termination_mask *= int(t < max_generation_len)

        tokens = tokens.reshape((bsize, num_samples, max_length))
        output_strs = [tokenizer.batch_decode(sample, skip_special_tokens=True) for sample in tokens]

        return [list(zip(output_strs[i], logit_scores[i].tolist())) for i in range(len(output_strs))]

    # def sample_raw(self,
    #                tokens: torch.Tensor,
    #                attn_mask: torch.Tensor,
    #                state_ids: torch.Tensor,
    #                action_ids: torch.Tensor,
    #                termination_condition: Callable[[np.ndarray], bool],
    #                num_generations=1, max_generation_len=None,
    #                temp=1.0, top_k=None, top_p=None,
    #                exp_adv=False, adv_weight=0.0, adv_clip=None,
    #                include_logits=True, include_adv=True,
    #                rerank_log_prob_weight: float = 0.0,
    #                rerank_advantage_weight: float = 0.0,
    #                ):
    #
    #     assert include_logits or include_adv
    #
    #     tokenizer = self.iql_model.dataset.tokenizer
    #     max_length = self.iql_model.dataset.max_len
    #     if max_length is None:
    #         max_length = self.iql_model.model.config.n_positions
    #     max_length = min(max_length, self.iql_model.model.config.n_positions)
    #
    #     device = self.iql_model.device
    #     bsize = tokens.shape[0]
    #
    #     n = bsize * num_generations
    #
    #     if max_generation_len is None:
    #         max_generation_len = max_length + 1
    #
    #     input_strs = [
    #         tokenizer.decode(tokens[i, :][:attn_mask[i, :].sum().long()].tolist(),
    #                          clean_up_tokenization_spaces=False)
    #         for i in range(len(tokens))]
    #
    #     model_outputs = self.iql_model(tokens, attn_mask, state_ids,
    #                                    action_ids,
    #                                    qv_kwargs={'use_cache': True},
    #                                    policy_kwargs={'use_cache': True},
    #                                    target_kwargs={'use_cache': True})['model_outputs']
    #
    #     kvs = {'qv': model_outputs['qv_model_outputs'].past_key_values}
    #
    #     if self.iql_model.lm_target is not None:
    #         kvs['target'] = model_outputs['target_model_outputs'].past_key_values
    #
    #     if self.iql_model.lm_policy is not None:
    #         kvs['policy'] = model_outputs['policy_model_outputs'].past_key_values
    #
    #     dialogue_lens = attn_mask.sum(dim=1)
    #
    #     tokens = pad_sequence(torch.repeat_interleave(tokens, num_generations, dim=0), max_length,
    #                           tokenizer.pad_token_id, device, 1)
    #
    #     dialogue_lens = torch.repeat_interleave(dialogue_lens, num_generations, dim=0)
    #
    #     kvs['qv'] = map_all_kvs(
    #         lambda x: pad_sequence(torch.repeat_interleave(x, num_generations, dim=0), max_length, 0.0, device, 2),
    #         kvs['qv'])
    #
    #     if 'target' in kvs:
    #         kvs['target'] = map_all_kvs(
    #             lambda x: pad_sequence(torch.repeat_interleave(x, num_generations, dim=0), max_length, 0.0, device, 2),
    #             kvs['target'])
    #
    #     if 'policy' in kvs:
    #         kvs['policy'] = map_all_kvs(
    #             lambda x: pad_sequence(torch.repeat_interleave(x, num_generations, dim=0), max_length, 0.0, device, 2),
    #             kvs['policy'])
    #
    #     log_probs = torch.full((dialogue_lens.shape[0],), 0.0).to(device)
    #     kls = torch.full((dialogue_lens.shape[0],),
    #                      math.log(num_generations) - ((num_generations - 1) / num_generations)).to(device)
    #
    #     advantages = torch.full((dialogue_lens.shape[0],), 0.0).to(device)
    #     termination_mask = torch.full((dialogue_lens.shape[0],), 1).to(device)
    #
    #     state_idxs_temp, action_idxs_temp = torch.zeros((dialogue_lens.shape[0], 1,)).long().to(device), torch.zeros(
    #         (dialogue_lens.shape[0], 1,)).long().to(device)
    #
    #     t = torch.min(dialogue_lens).int()
    #
    #     base_logits = torch.full((dialogue_lens.shape[0],), 0.0).to(device)
    #
    #     while termination_mask.sum() > 0 and t < max_length:
    #         curr_token = tokens[:, t - 1].unsqueeze(1)
    #
    #         curr_kvs = map_all_kvs(lambda x: x[:, :, :t - 1, :], kvs['qv'])
    #         curr_target_kvs, curr_policy_kvs = curr_kvs, curr_kvs
    #
    #         if 'target' in kvs:
    #             curr_target_kvs = map_all_kvs(lambda x: x[:, :, :t - 1, :], kvs['target'])
    #
    #         if 'policy' in kvs:
    #             curr_policy_kvs = map_all_kvs(lambda x: x[:, :, :t - 1, :], kvs['policy'])
    #
    #         # since we are using past_key_values, only need to provide a single token at a time
    #         # as we only want the Q/V values for the one token, state/action idxs gives us this
    #         iql_outputs = self.iql_model(curr_token, None, state_idxs_temp, action_idxs_temp,
    #                                      qv_kwargs={'use_cache': True, 'past_key_values': curr_kvs},
    #                                      policy_kwargs={'use_cache': True, 'past_key_values': curr_policy_kvs},
    #                                      target_kwargs={'use_cache': True, 'past_key_values': curr_target_kvs})
    #
    #         model_outputs, logits = iql_outputs['model_outputs'], iql_outputs['logits']
    #
    #         logits[:, 0, tokenizer.pad_token_id] = torch.where(termination_mask == 1, float('-inf'), 1e7)
    #
    #         logits[torch.arange(0, n).to(device), torch.full((n,), 0).to(device), tokens[:, t]] = logits[
    #             torch.arange(0, n).to(device), torch.full((n,), 0).to(device), tokens[:, t]].masked_fill_(
    #             t < dialogue_lens, 1e7)
    #
    #         edited_logits = process_logits(logits.clone(), temp=temp, top_k=top_k, top_p=top_p)
    #
    #         vs, qs = iql_outputs['target_vs'], iql_outputs['target_qs']
    #
    #         if exp_adv:
    #             adv_logits = adv_weight * (qs - vs.unsqueeze(2))
    #         else:
    #             adv_sign = ((qs - vs.unsqueeze(2)) > 0.0).float()
    #             adv_logits = adv_weight * adv_sign + (1 - adv_weight) * (1 - adv_sign)
    #             adv_logits = torch.log(adv_logits)
    #
    #         if adv_clip is not None:
    #             adv_logits = torch.clip(adv_logits, max=adv_clip)
    #
    #         adv_logits[:, 0, tokenizer.pad_token_id] = torch.where(termination_mask == 1, float('-inf'), 1e7)
    #
    #         adv_logits[torch.arange(0, n).to(device), torch.full((n,), 0).to(device), tokens[:, t]] = adv_logits[
    #             torch.arange(0, n).to(device), torch.full((n,), 0).to(device), tokens[:, t]].masked_fill_(
    #             t < dialogue_lens, 1e7)
    #
    #         full_logits = (edited_logits if include_logits else 0.0) + (
    #             adv_logits if include_adv else 0.0) + base_logits.unsqueeze(1).unsqueeze(2)
    #
    #         cat_dist = torch.distributions.categorical.Categorical(logits=full_logits[:, 0])
    #         original_cat_dist = torch.distributions.categorical.Categorical(logits=logits[:, 0])
    #
    #         new_tokens = cat_dist.sample()
    #         log_probs += cat_dist.log_prob(new_tokens)
    #         kls += (cat_dist.log_prob(new_tokens) - original_cat_dist.log_prob(new_tokens))
    #         qs_chosen = torch.gather(qs.squeeze(1), dim=1, index=new_tokens.unsqueeze(1)).squeeze(1)
    #         advantages += (qs_chosen - vs.squeeze(1))
    #         tokens[:, t] = new_tokens
    #
    #         kvs['qv'] = update_kvs(kvs['qv'], model_outputs['qv_model_outputs'].past_key_values,
    #                                torch.arange(0, n).to(device), t - 1)
    #         if 'target' in kvs:
    #             kvs['target'] = update_kvs(kvs['target'], model_outputs['target_model_outputs'].past_key_values,
    #                                        torch.arange(0, n).to(device), t - 1)
    #         if 'policy' in kvs:
    #             kvs['policy'] = update_kvs(kvs['policy'], model_outputs['policy_model_outputs'].past_key_values,
    #                                        torch.arange(0, n).to(device), t - 1)
    #
    #         for idx in range(n):
    #             if tokens[idx, t] == tokenizer.eoa_token_id and t >= dialogue_lens[idx]:
    #                 termination_mask[idx] *= (
    #                         1 - int(termination_condition(tokenizer.decode(tokens[idx, :].tolist(),
    #                                                                        clean_up_tokenization_spaces=False))))
    #         t += 1
    #         termination_mask *= ((t - dialogue_lens) < max_generation_len).int()
    #
    #     scores = ((advantages * rerank_advantage_weight) + (log_probs * rerank_log_prob_weight)).reshape(-1,
    #                                                                                                      num_generations)
    #     order = torch.argsort(-scores, dim=1)
    #     output_strs = [tokenizer.decode(tokens[i, :].tolist(), clean_up_tokenization_spaces=False) for i in
    #                    range(len(tokens))]
    #     processed_outputs = []
    #
    #     for i in range(len(input_strs)):
    #         temp_outputs = []
    #
    #         for x in range(num_generations):
    #             processed_str = output_strs[i * num_generations + order[i, x]][len(input_strs[i]):].strip()
    #             if tokenizer.id_to_token(tokenizer.pad_token_id) in processed_str:
    #                 processed_str = processed_str[
    #                                 :processed_str.find(tokenizer.id_to_token(tokenizer.pad_token_id))].strip()
    #             if tokenizer.id_to_token(tokenizer.eoa_token_id) in processed_str:
    #                 processed_str = processed_str[
    #                                 :processed_str.find(tokenizer.id_to_token(tokenizer.eoa_token_id))].strip()
    #             temp_outputs.append(processed_str)
    #
    #         processed_outputs.append(temp_outputs)
    #
    #     scores = torch.gather(scores, dim=1, index=order)
    #
    #     log_probs = torch.gather(log_probs.reshape(-1, num_generations), dim=1, index=order)
    #     kls = torch.gather(kls.reshape(-1, num_generations), dim=1, index=order)
    #
    #     return list(zip(input_strs, processed_outputs)), log_probs.reshape(-1, num_generations), kls
