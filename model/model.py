import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from .Embedding import PositionalEmbedding
from .MME import MultiModalEmbedding
from torch.nn import Transformer
from transformers import BertModel, AutoTokenizer

import random


# Seq2Seq Network
# [N,T,C] and [N,S,E] -> [N, len, vocab_size]
class VideoTransformer(nn.Module):
    def __init__(self,
                 num_encoder_layers: int,
                 num_decoder_layers: int,
                 feat_size: int,
                 emb_size: int,
                 nhead: int,
                 bert_type: str = "bert-base-uncased",
                 use_bert: bool = True,
                 dim_feedforward: int = 512,
                 dropout: float = 0.1,
                 device: torch.device = torch.device("cuda")
                 ):
        super(VideoTransformer, self).__init__()
        self.transformer = Transformer(d_model=emb_size,
                                       nhead=nhead,
                                       num_encoder_layers=num_encoder_layers,
                                       num_decoder_layers=num_decoder_layers,
                                       dim_feedforward=dim_feedforward,
                                       dropout=dropout,
                                       activation="gelu",
                                       batch_first=True)
        self.tokenizer = AutoTokenizer.from_pretrained("/data3/lzh_3/video-captioning-swin-transformer/data/tk/")
        pad_id = self.tokenizer.convert_tokens_to_ids("[PAD]")
        vocab_size = self.tokenizer.vocab_size
        self.generator = nn.Linear(emb_size, vocab_size)

        self.positional_encoding = PositionalEmbedding(emb_size, dropout=dropout)
        self.src_to_emb = nn.Linear(feat_size, emb_size)
        self.tgt_to_emb = nn.Embedding(vocab_size, emb_size, padding_idx=pad_id)

        self.emb_size = emb_size
        self.device = device
        self.use_bert = use_bert

    def forward(self,
                src: Tensor,
                tgt: Tensor,
                src_mask: Tensor,
                tgt_mask: Tensor,
                src_padding_mask: Tensor,
                tgt_padding_mask: Tensor,
                scheduled_sampling_rate=0.5):
        src_emb = self.positional_encoding(self.src_to_emb(src))  # src: torch.Size([16, 768, 20])

        # 如果是单个caption，则直接transformer
        if type(tgt) == Tensor:
            tgt_emb = self.positional_encoding(self.tgt_to_emb(tgt))
            outs = self.transformer(src_emb, tgt_emb, src_mask, tgt_mask, None,
                                    src_padding_mask, tgt_padding_mask, None)
            return self.generator(outs)
        # 如果是多个caption，则依次decode出结果
        elif type(tgt) == list:
            # 先得到encoder的输出
            memory = self.encoder(src, mask=src_mask, src_key_padding_mask=src_padding_mask)
            # 再一个一个视频decode
            tgts, tgt_masks, tgt_padding_masks = tgt, tgt_mask, tgt_padding_mask
            outputs = []
            for tgt, tgt_mask, tgt_padding_mask in zip([tgts, tgt_masks, tgt_padding_masks]):
                tgt_emb = self.positional_encoding(self.tgt_to_emb(tgt))
                output = self.decoder(tgt_emb, memory, tgt_mask=tgt_mask,
                                      tgt_key_padding_mask=tgt_padding_mask,
                                      memory_key_padding_mask=src_padding_mask)
                output = self.generator(output)
                outputs.append(output)
            return outputs

    def encode(self, src: Tensor):
        return self.transformer.encoder(self.positional_encoding(self.src_to_emb(src)))

    def decode(self, tgt: Tensor, memory: Tensor, tgt_mask: Tensor):
        if self.use_bert is True:
            return self.transformer.decoder(
                self.positional_encoding(self.tgt_to_emb(tgt).last_hidden_state.to(self.device)), memory, tgt_mask
            )
        else:
            return self.transformer.decoder(
                self.positional_encoding(self.tgt_to_emb(tgt)), memory, tgt_mask
            )

    def freeze_bert(self):
        assert self.use_bert is True
        for name, v in self.named_parameters():
            if "tgt_to_emb" in name:
                v.requires_grad = False

    def load_embedding_weights(self, weight):
        assert self.tgt_to_emb.weight.shape == weight.shape
        self.tgt_to_emb.weight = weight


class MMVideoTransformer(nn.Module):
    def __init__(self,
                 num_encoder_layers: int,
                 num_decoder_layers: int,
                 feat_dims: dict,
                 d_model: int,
                 nhead: int,
                 bert_type: str = "bert-base-uncased",
                 dim_feedforward: int = 512,
                 dropout: float = 0.1,
                 activation: str = "gelu",
                 device: torch.device = torch.device("cuda"),
                 agg_method: str = "avgpooling",
                 ):
        super(MMVideoTransformer, self).__init__()
        self.d_model = d_model
        self.device = device

        tokenizer = AutoTokenizer.from_pretrained(bert_type)
        self.tokenizer = tokenizer
        pad_id = tokenizer.convert_tokens_to_ids("[PAD]")
        vocab_size = tokenizer.vocab_size
        self.tgt_to_emb = nn.Embedding(vocab_size, d_model, padding_idx=pad_id)
        self.positional_encoding = PositionalEmbedding(d_model, dropout=dropout)

        self.mme = MultiModalEmbedding(d_model, feat_dims, dropout, agg_method=agg_method)

        self.mmt_encoder = nn.TransformerEncoder(
            nn.TransformerEncoderLayer(d_model, nhead, dim_feedforward, dropout, activation, batch_first=True),
            num_encoder_layers,
            nn.LayerNorm(d_model)
        )
        self.mmt_decoder = nn.TransformerDecoder(
            nn.TransformerDecoderLayer(d_model, nhead, dim_feedforward, dropout, activation, batch_first=True),
            num_decoder_layers,
            nn.LayerNorm(d_model)
        )

        self.generator = nn.Linear(d_model, self.tokenizer.vocab_size)

    def forward(self,
                feats_dict: dict,
                feats_padding_mask_dict: dict,
                tgt: Tensor,
                tgt_mask: Tensor,
                tgt_padding_mask: Tensor):
        # video features
        features, feats_padding_masks, lengths = self.mme(feats_dict, feats_padding_mask_dict)
        memories = self.mmt_encoder(
            features, mask=None, src_key_padding_mask=feats_padding_masks
        )

        # caption embeddings
        caption_emb = self.positional_encoding(self.tgt_to_emb(tgt))
        output = self.mmt_decoder(
            caption_emb, memories, tgt_mask=tgt_mask, tgt_key_padding_mask=tgt_padding_mask,
            memory_mask=None, memory_key_padding_mask=feats_padding_masks
        )

        return self.generator(output)
