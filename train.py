# -*- coding: utf-8 -*-
"""feedbackprize_v25b_train_01.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1Xk2d-jUx7lMPK8aJWkro3kBqDi_mr89G

# Version 25b
"""

!nvidia-smi

from google.colab import drive
drive.mount('/content/drive')

#!pip install transformers
#!pip install seqeval -qq
#!pip install datasets
#!pip install --upgrade pandas

"""# Packages"""

import os, gc, pickle, math, time, random, copy, shutil
from glob import glob
from tqdm.notebook import tqdm
from pylab import cm, matplotlib

import numpy as np
import pandas as pd

from sklearn.model_selection import KFold, StratifiedKFold, GroupKFold
from scipy.special import softmax
from spacy import displacy

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

# Mixed precision in Pytorch
from torch.cuda.amp import autocast, GradScaler

# Transformers
from transformers import get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup, AdamW
from transformers import AutoConfig, AutoTokenizer, AutoModel, AutoModelForTokenClassification
from transformers import DataCollatorForTokenClassification
from transformers import TrainingArguments, Trainer
from datasets import Dataset, DatasetDict, load_metric

from transformers.utils.logging import set_verbosity, WARNING
set_verbosity(WARNING)

import warnings
warnings.filterwarnings('ignore')
os.environ['TOKENIZERS_PARALLELISM'] = 'true'

pd.__version__

"""# Initial settings and configuration"""

class config(object):
    # General settings
    env = 'colab'
    seed = 2021
    use_tqdm = True
    apex = True
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    done_split = True
    debug = False
    verbose = 600
    # Validation
    nfolds = 5
    eval_every = np.inf
    # Data processing
    labels = ['Lead', 'Position', 'Evidence', 'Claim', 'Concluding Statement', 'Counterclaim', 'Rebuttal']
    shortest_string_len = 4
    # Dataset and DataLoader
    aug = ['masking', 'shuffling']    # None
    if aug is not None:
        aug_ratio = 0.2
        mask_ratio = 0.05
        shuffling_window = [2, 3]
    max_len = 1024
    max_infer_len = 1536
    stride = 128
    batch_size = 1
    num_workers = os.cpu_count()
    # Model
    backbone = 'microsoft/deberta-xlarge'
    tokenizer = AutoTokenizer.from_pretrained(backbone)
    model_name = 'v25b_microsoft_deberta_xlarge'
    attention_window = 512
    num_class = 15
    dropout = 0.
    # Training
    training_folds = [0, 1]
    nepochs = 5
    lr = 1e-5
    weight_decay = 1e-4
    gradient_accumulation_step = 2
    warm_up = 0.1
    label_smoothing = 0.05
    save_weights_only = True
    # Data paths
    if env == 'colab':
        data_dir = '/content/drive/My Drive/Kaggle competitions/FeedbackPrize/data'
        output_dir = '/content/drive/My Drive/Kaggle competitions/FeedbackPrize/model'
    elif env == 'kaggle':
        data_dir = '../input/feedback-prize-2021'
        output_dir = os.getcwd()
    elif env == 'jarvis':
        data_dir = 'data'
        output_dir = os.getcwd()
    os.makedirs(os.path.join(output_dir, model_name.split('_')[0][:-1], model_name.split('_')[0][-1]), exist_ok = True)
    
cfg = config()

def set_random_seed(seed, use_cuda = True):
    np.random.seed(seed) # cpu vars
    torch.manual_seed(seed) # cpu  vars
    random.seed(seed) # Python
    os.environ['PYTHONHASHSEED'] = str(seed) # Python hash building
    if use_cuda: 
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed) # gpu vars
        torch.backends.cudnn.deterministic = True  #needed
        torch.backends.cudnn.benchmark = False

"""# Set up logs"""

import logging
from imp import reload
reload(logging)
logging.basicConfig(
    level = logging.INFO,
    format = '%(asctime)s %(message)s',
    datefmt = '%H:%M:%S',
    handlers = [
        logging.FileHandler(f"train_{cfg.model_name}_{time.strftime('%m%d_%H%M', time.localtime())}_{cfg.seed}.log"),
        logging.StreamHandler()
    ]
)

logging.info(
    '\nmodel_name: {}\n'
    'env: {}\n'
    'seed: {}\n'
    'nfolds: {}\n'
    'max_len: {}\n'
    'batch_size: {}\n'
    'num_workers: {}\n'
    'nepochs: {}\n'
    'lr: {}\n'
    'weight_decay: {}\n'
    'gradient_accumulation_step: {}'.format(cfg.model_name, cfg.env, cfg.seed, cfg.nfolds, 
                                            cfg.max_len, cfg.batch_size, cfg.num_workers, cfg.nepochs, 
                                            cfg.lr, cfg.weight_decay, cfg.gradient_accumulation_step)
)

"""# Import data"""

if cfg.done_split:
    train_df = pd.read_csv(os.path.join(cfg.data_dir, f'train_{cfg.nfolds}fold_{cfg.seed}.csv'))
    id_fold_map = train_df[['id', 'kfold']].drop_duplicates()
    id_fold_map = dict(zip(id_fold_map.id.values, id_fold_map.kfold.values))
else:
    train_df = pd.read_csv(os.path.join(cfg.data_dir, 'train.csv'))
train_ids = train_df.id.unique()
train_df

"""# CV split"""

'''if cfg.done_split:
    folds = np.load(os.path.join(cfg.data_dir, f'folds_{cfg.nfolds}fold_{cfg.seed}.npy'))
    id_fold_map = dict(zip(train_ids, folds))
else:
    split = KFold(n_splits = cfg.nfolds, shuffle = True, random_state = cfg.seed)
    folds = -np.ones(train_ids.shape)
    
    for fold, (trn_idx, val_idx) in enumerate(split.split(train_ids)):
        folds[val_idx] = fold
        
    id_fold_map = dict(zip(train_ids, folds))
    train_df['kfold'] = train_df.id.map(id_fold_map)
    np.save(os.path.join(cfg.data_dir, f'folds_{cfg.nfolds}fold_{cfg.seed}.npy'), folds)
    train_df.to_csv(os.path.join(cfg.data_dir, f'train_{cfg.nfolds}fold_{cfg.seed}.csv'), index = None)
train_df'''

df = pd.read_pickle(os.path.join(cfg.data_dir, 'df.pkl'))
df['kfold'] = df['id'].map(id_fold_map)
df

"""# Label-to-index and Index-to-label dictionaries"""

from collections import defaultdict
tags = defaultdict()

for i, c in enumerate(cfg.labels):
    tags[f'B-{c}'] = i
    tags[f'I-{c}'] = i + len(cfg.labels)
tags[f'O'] = len(cfg.labels) * 2
tags[f'Special'] = -100
    
cfg.l2i = dict(tags)

i2l = defaultdict()
for k, v in cfg.l2i.items(): 
    i2l[v] = k
i2l[-100] = 'Special'

label_list = ['B-Lead', 'B-Position', 'B-Evidence', 'B-Claim', 'B-Concluding Statement', 'B-Counterclaim', 'B-Rebuttal',
              'I-Lead', 'I-Position', 'I-Evidence', 'I-Claim', 'I-Concluding Statement', 'I-Counterclaim', 'I-Rebuttal', 'O']
label_wt_factor = {'B-Counterclaim': 2.0, 'I-Counterclaim': 2.0, 'B-Rebuttal': 2.0, 'I-Rebuttal': 2.0}
cfg.label_wts = torch.tensor([label_wt_factor[l] if l in label_wt_factor.keys() else 1.0 for l in label_list], device = cfg.device)

cfg.i2l = dict(i2l)

"""# Dataset and DataLoader

* Data augmentation
"""

def aug_mask(cfg, input_ids, mask):
    num_ids = len(input_ids)
    all_idxs = list(range(num_ids))
    mask_idxs = random.choices(all_idxs, k = int(num_ids * cfg.mask_ratio))
    aug_input_ids = input_ids.copy()
    for idx in mask_idxs[:sum(mask)]:
        aug_input_ids[idx] = cfg.tokenizer.mask_token_id
    return aug_input_ids

def aug_shuffle(cfg, input_ids, labels):
    period_locations = np.where(np.array([4] + input_ids) == 4)[0]   # Locate the periods, assume they are sentence delimiters
    sentence_span = list(zip(period_locations[:-1], period_locations[1:]))   # Locate the span of each sentence
    sentence_input_ids = [input_ids[i:j] for (i, j) in sentence_span]    # Locate the input_ids of each sentence
    sentence_labels = [labels[i:j] for (i, j) in sentence_span]    # Locate the input_ids of each sentence

    ws = np.random.choice(cfg.shuffling_window)
    num_chunk = len(sentence_span) // ws

    idx = np.arange(0, len(sentence_span))
    aug_sentence_input_ids = []
    aug_sentence_labels = []
    for i in range(num_chunk):
        np.random.shuffle(idx[i * ws: (i + 1) * ws])
        for j in idx[i * ws: (i + 1) * ws]:
            aug_sentence_input_ids.append(sentence_input_ids[j])
            aug_sentence_labels.append(sentence_labels[j])
    
    aug_sentence_input_ids = [sentence for sublist in aug_sentence_input_ids for sentence in sublist]
    aug_sentence_labels = [sentence for sublist in aug_sentence_labels for sentence in sublist]

    # Padding or trimming
    if len(aug_sentence_input_ids) < cfg.max_len:
        aug_sentence_input_ids = aug_sentence_input_ids + [cfg.tokenizer.pad_token_id] * (cfg.max_len - len(aug_sentence_input_ids))
        aug_sentence_labels = aug_sentence_labels + [-100] * (cfg.max_len - len(aug_sentence_labels))

    return aug_sentence_input_ids, aug_sentence_labels

"""* Data prepration"""

def fix_beginnings(labels):
    for i in range(1, len(labels)):
        curr_lab = labels[i]
        prev_lab = labels[i-1]
        if curr_lab in range(7, 14):
            if prev_lab != curr_lab and prev_lab != curr_lab - 7:
                labels[i] = curr_lab - 7
    return labels

def preparing_train_data(example, cfg = cfg):
    token = cfg.tokenizer(example['text'], max_length = cfg.max_len, 
                          padding = 'max_length',
                          truncation = True, 
                          stride = cfg.stride,
                          return_overflowing_tokens = True, 
                          return_attention_mask = True, 
                          return_offsets_mapping = True)

    sample_mapping = token['overflow_to_sample_mapping']
    offset_mapping = token['offset_mapping']
    token['labels'] = []

    for i in range(len(offset_mapping)):
        input_ids = token['input_ids'][i]
        # Consider each chunk of this example
        sample_index = sample_mapping[i]
        labels = [cfg.l2i['O'] for i in range(len(input_ids))]

        for label_start, label_end, label in list(zip(example['starts'][sample_index], example['ends'][sample_index], example['classlist'][sample_index])):
            # In each chunk, consider the corresponding labels, loop over all tokens
            for j in range(len(labels)):
                token_start = offset_mapping[i][j][0]
                token_end = offset_mapping[i][j][1]
                if token_start == label_start: 
                    labels[j] = cfg.l2i[f'B-{label}']    
                elif token_start > label_start and token_end <= label_end:
                    labels[j] = cfg.l2i[f'I-{label}']

        # Convert the corresponding targets of the special tokens into -100, special token are: <s>: 0, <pad>: 1, </s>: 2, <unk>: 100
        for k, input_id in enumerate(input_ids):
            if input_id in [0, 1, 2]:
                labels[k] = -100
        
        labels = fix_beginnings(labels)

        # Augmentation
        if cfg.aug is not None:
            if 'masking' in cfg.aug:
                if random.random() < cfg.aug_ratio:
                    input_ids = aug_mask(cfg, input_ids, token['attention_mask'][i])
            
            if 'shuffling' in cfg.aug:
                if random.random() < cfg.aug_ratio:
                    input_ids, labels = aug_shuffle(cfg, input_ids, labels)

        token['input_ids'][i] = input_ids
        token['labels'].append(labels)
    return token

def preparing_valid_data(example, cfg = cfg):
    token = cfg.tokenizer(example['text'], max_length = cfg.max_infer_len,
                          truncation = True, 
                          return_attention_mask = True, 
                          return_offsets_mapping = True)
    
    offset_mapping = token['offset_mapping']
    token['labels'] = []

    for i in range(len(offset_mapping)):
        # Consider each chunk of this example
        labels = [cfg.l2i['O'] for i in range(len(token['input_ids'][i]))]

        for label_start, label_end, label in list(zip(example['starts'][i], example['ends'][i], example['classlist'][i])):
            # In each chunk, consider the corresponding labels, loop over all tokens
            for j in range(len(labels)):
                token_start = offset_mapping[i][j][0]
                token_end = offset_mapping[i][j][1]
                if token_start == label_start:
                    labels[j] = cfg.l2i[f'B-{label}']
                if token_start > label_start and token_end <= label_end:
                    labels[j] = cfg.l2i[f'I-{label}']

        # Convert the corresponding targets of the special tokens into -100, special token are: <s>: 0, <pad>: 1, </s>: 2
        for k, input_id in enumerate(token['input_ids'][i]):
            if input_id in [0, 1, 2]:
                labels[k] = -100
                
        labels = fix_beginnings(labels)        
        token['labels'].append(labels)
    return token

def preparing_infer_data(example, cfg = cfg):
    token = cfg.tokenizer(example['text'], max_length = cfg.max_infer_len, 
                          padding = 'max_length',
                          truncation = True, 
                          stride = cfg.stride,
                          return_overflowing_tokens = True, 
                          return_attention_mask = True, 
                          return_offsets_mapping = True)
    
    sample_index = token['overflow_to_sample_mapping']
    token['is_overflow'] = [False] + [i == j for i, j in zip(sample_index[:-1], sample_index[1:])]
    return token

def form_dataset(df, cfg, shuffle = True, fold = 0):
    if cfg.debug:
        df = df.sample(n = 1000)
    ds = Dataset.from_pandas(df)
    train_ds = ds.filter(lambda x: x['kfold'] != fold)
    valid_ds = ds.filter(lambda x: x['kfold'] == fold)
    split_ds = DatasetDict({
        'train': train_ds,
        'valid': valid_ds
    })
    tokenized_ds = split_ds.map(preparing_train_data, batched = True, batch_size = 20_000, remove_columns = split_ds['train'].column_names)
    if shuffle:
        train_ds = tokenized_ds['train'].shuffle(seed = cfg.seed)
        valid_ds = tokenized_ds['valid']
    else:
        train_ds = tokenized_ds['train']
        valid_ds = tokenized_ds['valid']
    return split_ds, train_ds, valid_ds

"""# Model"""

def clones(module, N = 2):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

class DropRNN(nn.Module):
    def __init__(self, rnn_type = 'GRU', input_size = 1024, hidden_size = 1024, num_layers = 2, dropout = 0.2, bidirectional = True):
        super(DropRNN, self).__init__()
        assert rnn_type in ['RNN', 'GRU', 'LSTM'], "The rnn_type argument only takes 'RNN', 'GRU', or 'LSTM' values!"
        self.rnn_type = rnn_type.upper()
        self.dropout = nn.Dropout(dropout)
        if bidirectional:
            hidden_size = hidden_size // 2 
        rnn_layer = getattr(nn, rnn_type)(input_size = input_size, hidden_size = hidden_size, bidirectional = bidirectional, batch_first = True)
        self.rnn_layers = clones(rnn_layer, num_layers)

    def forward(self, x):
        x = self.dropout(x)
        for layer in self.rnn_layers:
            x, _ = layer(x)
        return x

class FeedbackPrizeModel(nn.Module):
    def __init__(self, cfg, num_class = 15):
        super(FeedbackPrizeModel, self).__init__()
        self.config = AutoConfig.from_pretrained(cfg.backbone)
        self.config.attention_window = cfg.attention_window
        # self.config.gradient_checkpointing = True
        self.backbone = AutoModel.from_pretrained(cfg.backbone, config = self.config)
        self.main_head = nn.Linear(self.config.hidden_size, num_class)

        self.label_embed = nn.Embedding(num_embeddings = num_class, embedding_dim = self.config.hidden_size)
        self.labels = torch.tensor(list(range(num_class))).to(cfg.device)

        self.cosine_similarity = nn.CosineSimilarity(dim = -1)

        # Initialize
        self._init_weights(self.label_embed)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean = 0.0, std = self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean = 0.0, std = self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

    def forward(self, input_ids, attention_mask):
        bs, seq_len = input_ids.shape
        device = input_ids.device

        output_backbone = self.backbone(input_ids = input_ids, attention_mask = attention_mask)
        output = output_backbone[0]
        main_output = self.main_head(output)

        # Find the similarity between the sequence embeddings and label embeddings
        label_embeddings = self.label_embed(self.labels)    # (num_labels, embedding_dim)
        normed_label_embeddings = F.normalize(label_embeddings, dim = -1).unsqueeze(0).unsqueeze(0).repeat(bs, seq_len, 1, 1)
        norm_label_embeddings = torch.norm(label_embeddings, dim = -1)
        normed_sequence_embeddings = F.normalize(output, dim = -1).unsqueeze(2).repeat(1, 1, len(self.labels), 1)
        norm_sequence_embeddings = torch.norm(label_embeddings, dim = -1)
        
        aux_output = self.cosine_similarity(normed_sequence_embeddings, normed_label_embeddings) * norm_label_embeddings   # (bs, seq_len, num_labels)

        return main_output, aux_output

"""# Loss and metric functions

* Utils
"""

map_clip = {
    'Lead': 9, 
    'Position': 5, 
    'Evidence': 14, 
    'Claim': 3, 
    'Concluding Statement': 11,
    'Counterclaim': 6, 
    'Rebuttal': 4
}

proba_thresh = {
    'Lead': 0.7, 
    'Position': 0.55, 
    'Evidence': 0.65, 
    'Claim': 0.55, 
    'Concluding Statement': 0.7, 
    'Counterclaim': 0.5, 
    'Rebuttal': 0.55
}

ignore_chars = set([' ', '\xa0', '\x85'])

def get_class(c):
    if c == 2 * len(cfg.labels):
        return 'Other'
    elif c == -100:
        return 'Special'
    else:
        return cfg.i2l[c][2:]

def visualize(cfg, df, text):
    ents = []
    example = df['id'].loc[0]

    for i, row in df.iterrows():
        ents.append({
            'start': int(row['discourse_start']), 
            'end': int(row['discourse_end']), 
            'label': row['discourse_type'],
        })
        
    doc2 = {
        'text': text,
        'ents': ents,
        'title': example
    }
    
    options = {'ents': cfg.labels + ['Other'], 'colors': cfg.colors}
    displacy.render(doc2, style = 'ent', options = options, manual = True, jupyter = True)

def postprocess_ner_predictions(cfg, raw_example, processed_example, raw_predictions, from_logits = False):
    assert len(processed_example) == len(raw_predictions)
    predictions, pred_weights = [], []
    char_preds = None
    sample_index = processed_example['overflow_to_sample_mapping']
    mapping = processed_example['offset_mapping']
    is_overflow = processed_example['is_overflow']
    texts = raw_example['text']

    for idx, prediction in tqdm(enumerate(raw_predictions)):
        sample_idx = sample_index[idx]
        if not is_overflow[idx]:
            if char_preds is not None:
                if from_logits:
                    char_preds = softmax(char_preds, axis = 1)
                predictions.append(char_preds)
                pred_weights.append(char_wts)
            text = texts[sample_idx]
            if from_logits:
                char_preds = -10000.0 * np.ones((len(text), cfg.num_class), dtype = np.float32)
            else:
                char_preds = np.zeros((len(text), cfg.num_class), dtype = np.float32)
            char_wts = 1e-6 * np.ones(len(text), dtype = np.float32)
            char_start = 0

        for pred, offset in zip(prediction, mapping[idx]):
            if offset[0] < char_start or offset[0] == offset[1]:
                continue
            char_preds[offset[0]:offset[1]] = char_preds[offset[0]:offset[1]].clip(pred, None)
            wts = 1.0 / (offset[1] - offset[0])
            char_wts[offset[0]:offset[1]] = char_wts[offset[0]:offset[1]].clip(wts, None)
        
        char_start = np.max(mapping[idx])

    predictions.append(char_preds)
    pred_weights.append(char_wts)
    
    assert len(predictions) == np.unique(sample_index).size
    assert len(predictions) == len(pred_weights)
    
    return predictions, pred_weights

def pred2span(pred, pred_scores, pred_wts, example, viz = False):
    example_id = example['id']
    text = example['text']
    n_chars = len(pred)
    classes = []
    all_span = []
    phrase_scores, phrase_weights = [],[]
    prev_label = None
    for i, (c, s, w) in enumerate(zip(pred.tolist(), pred_scores.tolist(), pred_wts.tolist())):
        if i == 0:
            cur_span = [i, i + 1]
            classes.append(get_class(c))
            prev_label = c
            scores = [s]
            weights = [w]
        elif i > 0 and (c == prev_label or (c - 7) == prev_label):
            cur_span[1] = i + 1
            scores.append(s)
            weights.append(w)
        elif i > 0 and text[i] in ignore_chars:
            cur_span[1] = i + 1
        else:
            all_span.append(cur_span)
            cur_span = [i, i + 1]
            classes.append(get_class(c))
            prev_label = c
            phrase_scores.append(scores)
            phrase_weights.append(weights)
            scores = [s]
            weights = [w]
    all_span.append(cur_span)
    phrase_scores.append(scores)
    phrase_weights.append(weights)

    # Derive the prediction strings
    predstrings = []
    indices = []
    for i, (label, span, score, weight) in enumerate(zip(classes, all_span, phrase_scores, phrase_weights)):
        span_start = span[0]
        span_end = span[1]
        before = text[:span_start]
        token_start = len(before.split())
        if len(before) == 0:
            token_start = 0
        elif before[-1] != ' ':
            token_start -= 1
        num_tkns = len(text[span_start:span_end + 1].split())
        tkns = [str(x) for x in range(token_start, token_start + num_tkns)]
        predstring = ' '.join(tkns)
        if label != 'Other':
            if np.sum(np.array(score) * np.array(weight)) / sum(weight) >= proba_thresh[label]:
                predstrings.append(predstring)
                indices.append(i)
    
    classes = np.array(classes)[indices]
    all_span = np.array(all_span)[indices]
    
    assert len(classes) == len(predstrings)
    assert len(all_span) == len(predstrings)
                    
    rows = []
    for c, span, predstring in zip(classes, all_span, predstrings):
        e = {
            'id': example_id,
            'discourse_type': c,
            'predictionstring': predstring,
            'discourse_start': span[0],
            'discourse_end': span[1],
            'discourse': text[span[0]:span[1] + 1]
        }
        rows.append(e)

    if len(rows) == 0:
        return pd.DataFrame()
    else:
        df = pd.DataFrame(rows)
        df['length'] = df['discourse'].apply(lambda t: len(t.split()))
        min_tokens = df.discourse_type.map(map_clip) - 1
        df = df[df.length > min_tokens].reset_index(drop = True)
        if viz:
            visualize(df, text)
        return df

def get_pred_df(hard_predictions, soft_predictions, predictions_wts, dataset):
    pred_df = []
    for i in tqdm(range(len(dataset))):
        pred_df.append(pred2span(hard_predictions[i], soft_predictions[i], predictions_wts[i], dataset[i]))
    pred_df = pd.concat(pred_df, axis = 0).reset_index(drop = True)
    return pred_df

"""* Loss"""

def loss_fn(pred, true):
    main_pred, aux_pred = pred
    return (0.75 * nn.CrossEntropyLoss(ignore_index = -100, weight = cfg.label_wts)(main_pred.permute(0, 2, 1), true) +
            0.25 * nn.CrossEntropyLoss(ignore_index = -100, weight = cfg.label_wts)(aux_pred.permute(0, 2, 1), true))

"""* Metrics"""

metric = load_metric('seqeval')

def aux_metric_fn(p):
    pred, true = p
    pred = np.argmax(pred, axis = -1)

    # Remove ignored index (special tokens)
    true_predictions = [
        [cfg.i2l[p] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(pred, true)
    ]
    true_labels = [
        [cfg.i2l[l] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(pred, true)
    ]

    results = metric.compute(predictions = true_predictions, references = true_labels)
    return {
        'precision': results['overall_precision'],
        'recall': results['overall_recall'],
        'f1': results['overall_f1'],
        'accuracy': results['overall_accuracy'],
    }

# CODE FROM : Rob Mulla @robikscube
# https://www.kaggle.com/robikscube/student-writing-competition-twitch
def calc_overlap(row):
    """
    Calculates the overlap between prediction and
    ground truth and overlap percentages used for determining
    true positives.
    """
    set_pred = set(row.predictionstring_pred.split(' '))
    set_gt = set(row.predictionstring_gt.split(' '))
    # Length of each and intersection
    len_gt = len(set_gt)
    len_pred = len(set_pred)
    inter = len(set_gt.intersection(set_pred))
    overlap_1 = inter / len_gt
    overlap_2 = inter/ len_pred
    return [overlap_1, overlap_2]

def metric_fn(pred_df, gt_df):
    """
    A function that scores for the kaggle
        Student Writing Competition

    pred_df: predicted dataframe, need to have columns: (id, discourse_type, predictionstring)
    gt_df: ground-truth dataframe, need to have columns: (id, discourse_type, predictionstring)
        
    Uses the steps in the evaluation page here:
        https://www.kaggle.com/c/feedback-prize-2021/overview/evaluation
    """
    gt_df = gt_df[['id', 'discourse_type', 'predictionstring']] \
        .reset_index(drop = True).copy()
    pred_df = pred_df[['id', 'discourse_type', 'predictionstring']] \
        .reset_index(drop = True).copy()
    pred_df['pred_id'] = pred_df.index
    gt_df['gt_id'] = gt_df.index
    # Step 1. all ground truths and predictions for a given class are compared.
    joined = pred_df.merge(gt_df,
                           left_on = ['id', 'discourse_type'],
                           right_on = ['id', 'discourse_type'],
                           how = 'outer',
                           suffixes = ('_pred','_gt')
                          )
    joined['predictionstring_gt'] = joined['predictionstring_gt'].fillna(' ')
    joined['predictionstring_pred'] = joined['predictionstring_pred'].fillna(' ')

    joined['overlaps'] = joined.apply(calc_overlap, axis = 1)

    # 2. If the overlap between the ground truth and prediction is >= 0.5, 
    # and the overlap between the prediction and the ground truth >= 0.5,
    # the prediction is a match and considered a true positive.
    # If multiple matches exist, the match with the highest pair of overlaps is taken.
    joined['overlap1'] = joined['overlaps'].apply(lambda x: eval(str(x))[0])
    joined['overlap2'] = joined['overlaps'].apply(lambda x: eval(str(x))[1])


    joined['potential_TP'] = (joined['overlap1'] >= 0.5) & (joined['overlap2'] >= 0.5)
    joined['max_overlap'] = joined[['overlap1','overlap2']].max(axis = 1)
    tp_pred_ids = joined.query('potential_TP') \
        .sort_values('max_overlap', ascending = False) \
        .groupby(['id','predictionstring_gt']).first()['pred_id'].values

    # 3. Any unmatched ground truths are false negatives
    # and any unmatched predictions are false positives.
    fp_pred_ids = [p for p in joined['pred_id'].unique() if p not in tp_pred_ids]

    matched_gt_ids = joined.query('potential_TP')['gt_id'].unique()
    unmatched_gt_ids = [c for c in joined['gt_id'].unique() if c not in matched_gt_ids]

    # Get numbers of each type
    TP = len(tp_pred_ids)
    FP = len(fp_pred_ids)
    FN = len(unmatched_gt_ids)
    #calc microf1
    f1_score = TP / (TP + 0.5 * (FP + FN))
    return f1_score

def score_feedback_comp(pred_df, gt_df, return_class_scores = False):
    class_scores = {}
    pred_df = pred_df[['id', 'discourse_type', 'predictionstring']].reset_index(drop = True).copy()
    for discourse_type, gt_subset in gt_df.groupby('discourse_type'):
        pred_subset = (
            pred_df.loc[pred_df['discourse_type'] == discourse_type]
            .reset_index(drop = True)
            .copy()
        )
        class_score = metric_fn(pred_subset, gt_subset)
        class_scores[discourse_type] = class_score
    f1 = np.mean([v for v in class_scores.values()])
    if return_class_scores:
        return f1, class_scores
    return f1

"""# Trainer"""

class FeedbackPrizeTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs = False):
        input_ids = inputs['input_ids']
        attention_mask = inputs['attention_mask']
        labels = inputs['label']
            
        output = model(input_ids, attention_mask)
        loss = loss_fn(output, labels)
        return (loss, output) if return_outputs else loss

    def prediction_step(self, model, inputs, prediction_loss_only = False, ignore_keys = None):
        self.label_names.append('label')
        has_labels = all(inputs.get(k) is not None for k in self.label_names)

        input_ids = inputs['input_ids'].to(cfg.device)
        attention_mask = inputs['attention_mask'].to(cfg.device)

        with torch.no_grad():
            if has_labels:
                labels = inputs['label'].to(cfg.device)
                logits = model(input_ids, attention_mask)
                loss = loss_fn(logits, labels)
            else:
                labels = None
                logits = model(input_ids, attention_mask)
                loss = None

            main_logits, aux_logits = logits

            logits = 0.75 * main_logits + 0.25 * aux_logits

        if prediction_loss_only:
            return (loss, None, None)
                
        return (loss, logits, labels)

"""# Prepare training"""

def prepare_training(fold):
    save_path = os.path.join(cfg.output_dir, cfg.model_name.split('_')[0][:-1], cfg.model_name.split('_')[0][-1], f'fold_{fold}')
    os.makedirs(save_path, exist_ok = True)

    ds_config_dict = {
        "fp16": {
            "enabled": "auto",
            "loss_scale": 0,
            "loss_scale_window": 1000,
            "initial_scale_power": 16,
            "hysteresis": 2,
            "min_loss_scale": 1
        },

        "optimizer": {
            "type": "AdamW",
            "params": {
                "lr": "auto",
                "betas": "auto",
                "eps": "auto",
                "weight_decay": "auto"
            }
        },

        "scheduler": {
            "type": "WarmupLR",
            "params": {
                "warmup_min_lr": "auto",
                "warmup_max_lr": "auto",
                "warmup_num_steps": "auto"
            }
        },

        "zero_optimization": {
            "stage": 2,
            "offload_optimizer": {
                "device": "cpu",
                "pin_memory": True
            },
            "allgather_partitions": True,
            "allgather_bucket_size": 2e8,
            "overlap_comm": True,
            "reduce_scatter": True,
            "reduce_bucket_size": 5e8,
            "contiguous_gradients": True
        },

        "gradient_accumulation_steps": "auto",
        "gradient_clipping": "auto",
        "steps_per_print": 2000,
        "train_batch_size": "auto",
        "train_micro_batch_size_per_gpu": "auto",
        "wall_clock_breakdown": False
    }

    training_args = TrainingArguments(
        output_dir = save_path,
        logging_dir = os.getcwd(),
        logging_strategy = 'epoch',
        save_strategy = 'steps',
        save_steps = 1297,
        learning_rate = cfg.lr,
        lr_scheduler_type = 'cosine',
        per_device_train_batch_size = cfg.batch_size,
        per_device_eval_batch_size = cfg.batch_size,
        num_train_epochs = cfg.nepochs,
        weight_decay = cfg.weight_decay,
        warmup_ratio = cfg.warm_up,
        gradient_accumulation_steps = cfg.gradient_accumulation_step,
        label_smoothing_factor = cfg.label_smoothing,
        # deepspeed = ds_config_dict
    )

    split_ds, train_dataset, valid_dataset = form_dataset(df, cfg, shuffle = True, fold = fold)
    try:
        train_dataset = train_dataset.rename_column('labels', 'label')
        valid_dataset = valid_dataset.rename_column('labels', 'label')
    except:
        pass

    data_collator = DataCollatorForTokenClassification(cfg.tokenizer)
    model = FeedbackPrizeModel(cfg)
    metric = load_metric('seqeval')

    trainer = FeedbackPrizeTrainer(
            model,
            training_args,
            train_dataset = train_dataset,
            eval_dataset = valid_dataset,
            tokenizer = cfg.tokenizer,
            data_collator = data_collator,
            compute_metrics = aux_metric_fn
        )

    return split_ds, trainer

"""# Prepare validating"""

def prepare_validating(split_ds, model):
    # Create validation dataset
    valid_dataset = split_ds['valid'].map(preparing_infer_data, fn_kwargs = {'cfg': cfg}, batched = True, remove_columns = split_ds['train'].column_names, batch_size = 20_000)
    try:
        valid_dataset = valid_dataset.rename_column('labels', 'label')
    except:
        pass

    # Create the ground-truth dataset
    gt_df = []
    for example in split_ds['valid']:
        for c, p in list(zip(example['classlist'], example['predictionstrings'])):
            gt_df.append({
                'id': example['id'],
                'discourse_type': c,
                'predictionstring': p,
            })
        
    gt_df = pd.DataFrame(gt_df)

    data_collator = DataCollatorForTokenClassification(cfg.tokenizer)
    trainer = FeedbackPrizeTrainer(
            model,
            tokenizer = cfg.tokenizer,
            data_collator = data_collator,
            compute_metrics = aux_metric_fn
        )
    
    return valid_dataset, trainer, gt_df

"""# Main"""

def main():
    final_pred = []
    raw_pred = []
    ids = []

    for fold in cfg.training_folds:
        logging.info(f' Fold {fold} '.center(30, '*'))
        set_random_seed(cfg.seed + fold, torch.cuda.is_available())

        # Preparing the datasets and the trainer
        split_ds, trainer = prepare_training(fold)

        # Train
        trainer.train()
        
        # Validating
        storage_dir = os.path.join(cfg.output_dir, cfg.model_name.split('_')[0][:-1], cfg.model_name.split('_')[0][-1], f'fold_{fold}')
        ckps = [os.path.join(storage_dir, i) for i in os.listdir(storage_dir) if i != 'runs']
        
        best_valid_score = -np.inf
        for i, ckp in enumerate(ckps):
            model = FeedbackPrizeModel(cfg)
            model.load_state_dict(torch.load(os.path.join(ckp, 'pytorch_model.bin'), map_location = cfg.device))
            model.eval()

            # Preparing the valid dataset and ground-truth labels
            logging.info('Inferring ...')
            valid_dataset, trainer, gt_df = prepare_validating(split_ds, model)
            predictions, _, _ = trainer.predict(valid_dataset)

            logging.info('Inferring the character soft and hard labels...')
            char_predictions, char_prediction_wts = postprocess_ner_predictions(cfg, split_ds['valid'], valid_dataset, predictions, from_logits = True)
            
            soft_char_predictions = [np.max(p, axis = -1) for p in char_predictions]
            hard_char_predictions = [np.argmax(p, axis = -1) for p in char_predictions]

            logging.info('Inferring the character soft and hard labels...')
            pred_df = get_pred_df(hard_char_predictions, soft_char_predictions, char_prediction_wts, split_ds['valid'])
            f1, component_f1 = score_feedback_comp(pred_df, gt_df, return_class_scores = True)
            
            logging.info(f'Global F1 {f1}')
            logging.info(f'Component F1 {component_f1}')

            # Pad or truncate the soft predictions
            padded_soft_predictions = np.zeros((predictions.shape[0], cfg.max_len, cfg.num_class))
            if predictions.shape[1] > cfg.max_len:
                # Truncate the soft_predictions
                padded_soft_predictions = predictions[:,:cfg.max_len,:]
            elif predictions.shape[1] < cfg.max_len:
                # Pad
                padded_soft_predictions[:,:predictions.shape[1],:] = predictions
            else:
                padded_soft_predictions = predictions
            
            if f1 > best_valid_score:
                best_valid_score = f1
                best_valid_component_score = component_f1
                best_ckp = ckp
                best_epoch = i
                best_final_pred = pred_df
                best_raw_pred = padded_soft_predictions
                best_ids = split_ds['valid']['id']

        final_pred.append(best_final_pred)
        raw_pred.append(best_raw_pred)
        ids.append(best_ids)
        
        logging.info('-' * 30)
        logging.info(f'Best Valid Score: {best_valid_score} - At epoch: {best_epoch}')
        logging.info(f'Best Valid Component Score: {best_valid_component_score}')
        logging.info(f'Best checkpoint name: {best_ckp}')

        ckps.remove(best_ckp)

        for ckp in ckps:
            shutil.rmtree(ckp)

if __name__ == '__main__':
    main()