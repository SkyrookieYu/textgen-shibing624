# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: 
"""
import random
import torch
import numpy as np
import os
import pickle
from multiprocessing import Pool

import torch.nn.functional as F
from loguru import logger
from torch.utils.data import Dataset
from tqdm.auto import tqdm

PAD, UNK, BOS, EOS = '<pad>', '<unk>', '<bos>', '<eos>'
BOC, EOC = '<boc>', '<eoc>'
LS, RS, SP = '<s>', '</s>', ' '
CS = ['<c-1>'] + ['<c' + str(i) + '>' for i in range(32)]  # content
SS = ['<s-1>'] + ['<s' + str(i) + '>' for i in range(512)]  # segment
PS = ['<p-1>'] + ['<p' + str(i) + '>' for i in range(512)]  # position
TS = ['<t-1>'] + ['<t' + str(i) + '>' for i in range(32)]  # other types
PUNCS = {",", ".", "?", "!", ":", "，", "。", "？", "！", "："}
BUFSIZE = 4096000


class ZHCharTokenizer(object):
    def __init__(self, vocab_file, min_occur_cnt=1, specials=None):
        special_tokens = [PAD, UNK, BOS, EOS, BOC, EOC, LS, RS]
        idx2token = special_tokens + [SP] + CS + SS + PS + TS \
                    + (specials if specials is not None else [])
        idx2token += self.load_vocab(vocab_file, min_occur_cnt)
        self._token2idx = dict(zip(idx2token, range(len(idx2token))))
        self._idx2token = idx2token
        self._padding_idx = self._token2idx[PAD]
        self._unk_idx = self._token2idx[UNK]
        self.special_tokens = special_tokens

    @staticmethod
    def load_vocab(vocab_file, min_occur_cnt=1):
        vocabs = []
        with open(vocab_file, encoding='utf8') as f:
            for line in f:
                line = line.strip('\n')
                if line:
                    terms = line.split('\t')
                    if len(terms) == 2:
                        if int(terms[1]) >= min_occur_cnt and terms[0]:
                            vocabs.append(terms[0])
                    else:
                        vocabs.append(line)
        return vocabs

    @property
    def size(self):
        return len(self._idx2token)

    @property
    def unk_idx(self):
        return self._unk_idx

    @property
    def padding_idx(self):
        return self._padding_idx

    def random_token(self):
        return self.idx2token(1 + np.random.randint(self.size - 1))

    def idx2token(self, x):
        if isinstance(x, list):
            return [self.idx2token(i) for i in x]
        return self._idx2token[x]

    def token2idx(self, x):
        if isinstance(x, list):
            return [self.token2idx(i) for i in x]
        return self._token2idx.get(x, self.unk_idx)

    @classmethod
    def from_pretrained(cls, vocab_file, min_occur_cnt=1, *init_inputs, **kwargs):
        r"""
        Instantiate a predefined tokenizer.
        """
        try:
            tokenizer = cls(vocab_file, min_occur_cnt=min_occur_cnt, *init_inputs, **kwargs)
        except OSError:
            raise OSError(
                "Unable to load vocabulary from file. "
                "Please check that the provided vocabulary is accessible and not corrupted."
            )
        return tokenizer

    def save_pretrained(self, vocab_file):
        r"""
        Save vocab.
        """
        with open(vocab_file, 'w', encoding='utf8') as f:
            for token, idx in self._token2idx.items():
                f.write(token + '\n')
            logger.info("Vocab saved in {}".format(vocab_file))


def lists2tensor(xs, tokenizer=None):
    max_len = max(len(x) for x in xs)
    ys = []
    for x in xs:
        if tokenizer is not None:
            y = tokenizer.token2idx(x) + [tokenizer.padding_idx] * (max_len - len(x))
        else:
            y = x + [0] * (max_len - len(x))
        ys.append(y)
    return ys


def batchify(data, tokenizer):
    xs_tpl, xs_seg, xs_pos, \
    ys_truth, ys_inp, \
    ys_tpl, ys_seg, ys_pos, msk = [], [], [], [], [], [], [], [], []
    for xs_tpl_i, xs_seg_i, xs_pos_i, ys_i, ys_tpl_i, ys_seg_i, ys_pos_i in data:
        xs_tpl.append(xs_tpl_i)
        xs_seg.append(xs_seg_i)
        xs_pos.append(xs_pos_i)

        ys_truth.append(ys_i)
        ys_inp.append([BOS] + ys_i[:-1])
        ys_tpl.append(ys_tpl_i)
        ys_seg.append(ys_seg_i)
        ys_pos.append(ys_pos_i)

        msk.append([1 for i in range(len(ys_i))])

    xs_tpl = torch.LongTensor(lists2tensor(xs_tpl, tokenizer)).t_().contiguous()
    xs_seg = torch.LongTensor(lists2tensor(xs_seg, tokenizer)).t_().contiguous()
    xs_pos = torch.LongTensor(lists2tensor(xs_pos, tokenizer)).t_().contiguous()
    ys_truth = torch.LongTensor(lists2tensor(ys_truth, tokenizer)).t_().contiguous()
    ys_inp = torch.LongTensor(lists2tensor(ys_inp, tokenizer)).t_().contiguous()
    ys_tpl = torch.LongTensor(lists2tensor(ys_tpl, tokenizer)).t_().contiguous()
    ys_seg = torch.LongTensor(lists2tensor(ys_seg, tokenizer)).t_().contiguous()
    ys_pos = torch.LongTensor(lists2tensor(ys_pos, tokenizer)).t_().contiguous()
    msk = torch.FloatTensor(lists2tensor(msk)).t_().contiguous()
    return xs_tpl, xs_seg, xs_pos, ys_truth, ys_inp, ys_tpl, ys_seg, ys_pos, msk


def s2t(strs, tokenizer):
    inp, msk = [], []
    for x in strs:
        inp.append(x)
        msk.append([1 for i in range(len(x))])

    inp = torch.LongTensor(lists2tensor(inp, tokenizer)).t_().contiguous()
    msk = torch.FloatTensor(lists2tensor(msk)).t_().contiguous()
    return inp, msk


def s2xy(lines, tokenizer, max_len, min_len):
    data = []
    for line in lines:
        res = parse_line(line, max_len, min_len)
        if not res:
            continue
        data.append(res)
    return batchify(data, tokenizer)


def parse_line(line, max_len, min_len=2):
    line = line.strip()
    if not line:
        return None
    fs = line.split("<s2>")
    author, cipai = fs[0].split("<s1>")
    sents = fs[1].strip()
    if len(sents) > max_len:
        sents = sents[:max_len]
    if len(sents) < min_len:
        return None
    sents = sents.split("</s>")

    ys = []
    xs_tpl = []
    xs_seg = []
    xs_pos = []

    ctx = cipai
    ws = [w for w in ctx]
    xs_tpl = ws + [EOC]
    xs_seg = [SS[0] for w in ws] + [EOC]
    xs_pos = [SS[i + 300] for i in range(len(ws))] + [EOC]

    ys_tpl = []
    ys_seg = []
    ys_pos = []
    for si, sent in enumerate(sents):
        ws = []
        sent = sent.strip()
        if not sent:
            continue
        for w in sent:
            ws.append(w)
            if w.strip() and w not in PUNCS:
                ys_tpl.append(CS[2])
            else:
                ys_tpl.append(CS[1])
        ys += ws + [RS]
        if ws[-1] in PUNCS:
            ys_tpl[-2] = CS[3]
        else:
            ys_tpl[-1] = CS[3]
        ys_tpl += [RS]
        ys_seg += [SS[si + 1] for w in ws] + [RS]
        ys_pos += [PS[len(ws) - i] for i in range(len(ws))] + [RS]

    ys += [EOS]
    ys_tpl += [EOS]
    ys_seg += [EOS]
    ys_pos += [EOS]

    xs_tpl += ys_tpl
    xs_seg += ys_seg
    xs_pos += ys_pos

    if len(ys) < min_len:
        return None
    return xs_tpl, xs_seg, xs_pos, ys, ys_tpl, ys_seg, ys_pos


def s2xy_polish(lines, tokenizer, max_len, min_len=2):
    data = []
    for line in lines:
        res = parse_line_polish(line, max_len, min_len)
        data.append(res)
    return batchify(data, tokenizer)


def parse_line_polish(line, max_len, min_len):
    line = line.strip()
    if not line:
        return None
    fs = line.split("<s2>")
    author, cipai = fs[0].split("<s1>")
    sents = fs[1].strip()
    if len(sents) > max_len:
        sents = sents[:max_len]
    if len(sents) < min_len:
        return None
    sents = sents.split("</s>")

    ys = []
    xs_tpl = []
    xs_seg = []
    xs_pos = []

    ctx = cipai
    ws = [w for w in ctx]
    xs_tpl = ws + [EOC]
    xs_seg = [SS[0] for w in ws] + [EOC]
    xs_pos = [SS[i + 300] for i in range(len(ws))] + [EOC]

    ys_tpl = []
    ys_seg = []
    ys_pos = []
    for si, sent in enumerate(sents):
        ws = []
        sent = sent.strip()
        if not sent:
            continue
        for w in sent:
            ws.append(w)
            if w == "_":
                ys_tpl.append(CS[2])
            else:
                ys_tpl.append(w)
        ys += ws + [RS]
        ys_tpl += [RS]
        ys_seg += [SS[si + 1] for w in ws] + [RS]
        ys_pos += [PS[len(ws) - i] for i in range(len(ws))] + [RS]

    ys += [EOS]
    ys_tpl += [EOS]
    ys_seg += [EOS]
    ys_pos += [EOS]

    xs_tpl += ys_tpl
    xs_seg += ys_seg
    xs_pos += ys_pos

    if len(ys) < min_len:
        return None

    return xs_tpl, xs_seg, xs_pos, ys, ys_tpl, ys_seg, ys_pos


class DataLoader(object):
    def __init__(self, tokenizer, filename, batch_size, max_len_y, min_len_y):
        self.batch_size = batch_size
        self.tokenizer = tokenizer
        self.max_len_y = max_len_y
        self.min_len_y = min_len_y
        self.filename = filename
        self.stream = open(self.filename, encoding='utf8')
        self.epoch_id = 0

    def __iter__(self):

        lines = self.stream.readlines(BUFSIZE)

        if not lines:
            self.epoch_id += 1
            self.stream.close()
            self.stream = open(self.filename, encoding='utf8')
            lines = self.stream.readlines(BUFSIZE)

        data = []
        for line in lines:
            res = parse_line(line, self.max_len_y, self.min_len_y)
            if not res:
                continue
            data.append(res)

        random.shuffle(data)

        idx = 0
        while idx < len(data):
            yield batchify(data[idx:idx + self.batch_size], self.tokenizer)
            idx += self.batch_size


def preprocess_data(data):
    prefix, input_text, target_text, tokenizer, args = data
    data_one = parse_line(input_text, args.max_length)
    xs_tpl, xs_seg, xs_pos, ys_truth, ys_inp, ys_tpl, ys_seg, ys_pos, msk = batchify([data_one], tokenizer)
    return xs_tpl[0], xs_seg[0], xs_pos[0], ys_truth[0], ys_inp[0], ys_tpl[0], ys_seg[0], ys_pos[0], msk[0]


class SongNetDataset(Dataset):
    def __init__(self, tokenizer, args, data, mode):
        cached_features_file = os.path.join(
            args.cache_dir,
            args.model_name.replace("/", "_")
            + "_cached_"
            + str(args.max_seq_length)
            + str(len(data)),
        )

        if os.path.exists(cached_features_file) and (
                (not args.reprocess_input_data and not args.no_cache)
                or (mode == "dev" and args.use_cached_eval_features and not args.no_cache)
        ):
            logger.info(" Loading features from cached file %s" % cached_features_file)
            with open(cached_features_file, "rb") as handle:
                self.examples = pickle.load(handle)
        else:
            logger.info(" Creating features from dataset file at %s" % args.cache_dir)

            data = [
                (prefix, input_text, target_text, tokenizer, args)
                for prefix, input_text, target_text in zip(
                    data["prefix"], data["input_text"], data["target_text"]
                )
            ]
            self.examples = [preprocess_data(d) for d in tqdm(data, disable=args.silent)]
            if not args.no_cache:
                logger.info(" Saving features into cached file %s" % cached_features_file)
                with open(cached_features_file, "wb") as handle:
                    pickle.dump(self.examples, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, index):
        return self.examples[index]
