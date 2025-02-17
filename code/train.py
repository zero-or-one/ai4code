import json
from pathlib import Path
from dataset import *
import numpy as np
import pandas as pd
from torch.utils.data import DataLoader, Dataset, SubsetRandomSampler, Subset
from model import *
from tqdm import tqdm
import sys, os
from metrics import *
import torch
import random
import argparse
from omegaconf import DictConfig, OmegaConf
from sklearn.model_selection import StratifiedKFold, KFold

os.environ["CUDA_VISIBLE_DEVICES"]='3,4'

def seed_everything(seed: int):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

def read_data(data):
    return tuple(d.cuda() for d in data[:-1]), data[-1].cuda()


def validate(model, val_loader):
    model.eval()

    tbar = tqdm(val_loader, file=sys.stdout)

    preds = []
    labels = []

    with torch.no_grad():
        for idx, data in enumerate(tbar):
            inputs, target = read_data(data)

            with torch.cuda.amp.autocast():
                pred = model(*inputs)

            preds.append(pred.detach().cpu().numpy().ravel())
            labels.append(target.detach().cpu().numpy().ravel())

    return np.concatenate(labels), np.concatenate(preds)


def train(model, optimizer, scheduler, train_loader, val_loader, epochs, fold):
    criterion = torch.nn.L1Loss()
    scaler = torch.cuda.amp.GradScaler()

    for e in range(config.epochs):
        model.train()
        tbar = tqdm(train_loader, file=sys.stdout)
        loss_list = []
        preds = []
        labels = []

        for idx, data in enumerate(tbar):
            inputs, target = read_data(data)

            with torch.cuda.amp.autocast():
                pred = model(*inputs)
                loss = criterion(pred, target)
            scaler.scale(loss).backward()
            if idx % config.accumulation_steps == 0 or idx == len(tbar) - 1:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                scheduler.step()

            loss_list.append(loss.detach().cpu().item())
            preds.append(pred.detach().cpu().numpy().ravel())
            labels.append(target.detach().cpu().numpy().ravel())

            avg_loss = np.round(np.mean(loss_list), 4)

            tbar.set_description(f"Epoch {e + 1} Loss: {avg_loss} lr: {scheduler.get_last_lr()}")

        torch.save(model.state_dict(), f'../../outputs/{config.name}_model_{fold}_{e}.bin')
        torch.save(optimizer.state_dict(), f'../../outputs/{config.name}_optim_{fold}_{e}.bin')
        torch.save(scheduler.state_dict(), f'../../outputs/{config.name}_sched_{fold}_{e}.bin')
        
    y_val, y_pred = validate(model, val_loader)
    val_df["pred"] = val_df.groupby(["id", "cell_type"])["rank"].rank(pct=True)
    val_df.loc[val_df["cell_type"] == "markdown", "pred"] = y_pred
    y_dummy = val_df.sort_values("pred").groupby('id')['cell_id'].apply(list)
    print(f"Preds score for fold {fold}", kendall_tau(df_orders.loc[y_dummy.index], y_dummy))

    return model, y_pred




if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process some arguments')
    parser.add_argument('--config', type=str, default='../configs/codebert-base.yaml')
    parser.add_argument('--train_mark_path', type=str, default='../../data/train_mark.csv')
    parser.add_argument('--train_features_path', type=str, default='../../data/train_fts.json')
    parser.add_argument('--val_mark_path', type=str, default='../../data/val_mark.csv')
    parser.add_argument('--val_features_path', type=str, default='../../data/val_fts.json')
    parser.add_argument('--val_path', type=str, default="../../data/val.csv")
    parser.add_argument('--load_model', type=bool, default=False)
    parser.add_argument('--model_path', type=str, default="../../outputs/codebert_base.bin")
    
    args = parser.parse_args()
    config = OmegaConf.load(args.config)
    seed_everything(config.seed)

    if (not os.path.exists("../../outputs")):
      os.mkdir("../../outputs")
    data_dir = Path('../../input/')
    
    train_df_mark = pd.read_csv(args.train_mark_path).drop("parent_id", axis=1).dropna().reset_index(drop=True)
    train_fts = json.load(open(args.train_features_path))
    val_df_mark = pd.read_csv(args.val_mark_path).drop("parent_id", axis=1).dropna().reset_index(drop=True)
    val_fts = json.load(open(args.val_features_path))
    val_df = pd.read_csv(args.val_path)
    
    order_df = pd.read_csv("../../input/train_orders.csv").set_index("id")
    df_orders = pd.read_csv(
        data_dir / 'train_orders.csv',
        index_col='id',
        squeeze=True,
    ).str.split()
    
    

    val_ds = MarkdownDataset(val_df_mark, model_name_or_path=config.model_name, md_max_len=config.md_max_len,
                             total_max_len=config.total_max_len, fts=val_fts)
    val_loader = DataLoader(val_ds, batch_size=config.batch_size, shuffle=False, num_workers=config.n_workers,
                                pin_memory=False, drop_last=False)
                                
    cv = KFold(n_splits=config.num_folds, shuffle=True, random_state=config.seed)
    fold_index = config.fold_index
    folds = list(cv.split(np.arange(len(train_df_mark))))                            
    for fold in fold_index:
        print('------------fold no---------{}----------------------'.format(fold))
        train_idx, val_idx = folds[fold] # we will not use val for now
        print(len(train_idx), len(val_idx))
        
        train_ds = MarkdownDataset(train_df_mark.iloc[val_idx], model_name_or_path=config.model_name, md_max_len=config.md_max_len,
                                   total_max_len=config.total_max_len, fts=train_fts)
        #train_subsampler = SubsetRandomSampler(train_idx)
        #subset_train = Subset(train_ds, train_idx)
        train_loader = DataLoader(train_ds, batch_size=config.batch_size, num_workers=config.n_workers,
                                  pin_memory=False, drop_last=True)

        # CREATE PARAMETERS
        model = MarkdownModel(config.model_name)
        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
    
        num_train_optimization_steps = int(config.epochs * len(train_loader) / config.accumulation_steps)
        optimizer = AdamW(optimizer_grouped_parameters, lr=config.lr, correct_bias=False) 
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0.05 * num_train_optimization_steps,
                                                    num_training_steps=num_train_optimization_steps)  
        # LOAD MODEL
        if args.load_model:
          # TODO: LOAD EVERYTHING
          print("Loading model")
          ckpt = torch.load(args.model_path)
          model.load_state_dict(ckpt)
        model = torch.nn.DataParallel(model.cuda())
        model, y_pred = train(model, optimizer, scheduler, train_loader, val_loader, epochs=config.epochs, fold=fold)
