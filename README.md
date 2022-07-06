### Preprocessing
To extract features for training, including the markdown-only dataframes and sampling the code cells needed for each note book, simply run:

```$ python preprocess.py```

Your outputs will be in the ```./data``` folder:
```
project
│   train_mark.csv
│   train_fts.json   
|   train.csv
│   val_mark.csv
│   val_fts.json
│   val.csv
```

DONE:
* Write the baseline code


###  TODO
* Add fold training
* Find different model configurations

### Results
| Model                         | CV      | LR      |
| ----------------------------- |:-------:|:-------:|
| [1]: codebert_base            | -       | -       |