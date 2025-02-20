import logging
import os
import re

import torch
from pymongo import MongoClient
from tqdm import tqdm

from data.HOList.utils import prooflog_to_examples, io_util
from data.HOList.utils.sexpression_to_graph import sexpression_to_graph
from experiments.HOList import deephol_pb2
from experiments.HOList.distributed_loop import options_pb2


def tokenize_string(string):
    pattern = r'(\(|\)|\s)'
    tokens = re.split(pattern, string)
    tokens = [token for token in tokens if token.strip()]  # Remove empty tokens
    return tokens

def gen_vocab_dict(vocab_file):
    with open(vocab_file) as f:
        x = f.readlines()
    vocab = {}
    for a, b in enumerate(x):
        vocab[b.replace("\n", "")] = a
    return vocab

def prepare_data(config):
    tac_dir = config['tac_dir']
    theorem_dir = config['theorem_dir']
    human_train_logs = config['human_train_logs']
    synthetic_train_logs = config['synthetic_train_logs']
    val_logs = config['val_logs']
    vocab_file = config['vocab_file']
    source = config['source']
    data_options = config['data_options']
    #
    logging.info('Generating data..')
    #
    scrub_parameters = options_pb2.ConvertorOptions.NOTHING
    #
    logging.info('Loading theorem database..')
    theorem_db = io_util.load_theorem_database_from_file(theorem_dir)
    #
    human_train_logs = io_util.read_protos(human_train_logs, deephol_pb2.ProofLog)
    synthetic_train_logs = io_util.read_protos(synthetic_train_logs, deephol_pb2.ProofLog)
    val_logs = io_util.read_protos(val_logs, deephol_pb2.ProofLog)

    options = options_pb2.ConvertorOptions(tactics_path=tac_dir, scrub_parameters=scrub_parameters)

    converter = prooflog_to_examples.create_processor(options=options, theorem_database=theorem_db)
    #
    logging.info('Loading human proof logs..')
    human_processed_logs = []
    for j, i in tqdm(enumerate(converter.process_proof_logs(human_train_logs))):
        human_processed_logs.append(i)
    #
    logging.info('Loading synthetic proof logs..')
    synthetic_processed_logs = []
    for j, i in tqdm(enumerate(converter.process_proof_logs(synthetic_train_logs))):
        synthetic_processed_logs.append(i)

    logging.info('Loading validation logs')
    val_processed_logs = []
    for j, i in tqdm(enumerate(converter.process_proof_logs(val_logs))):
        val_processed_logs.append(i)
    #
    human_train_params = []
    synthetic_train_params = []
    val_params = []
    #
    for a in human_processed_logs:
        human_train_params.extend(a['thms'])
    for a in val_processed_logs:
        val_params.extend(a['thms'])
    for a in synthetic_processed_logs:
        synthetic_train_params.extend(a['thms'])
    #
    all_params = human_train_params + val_params + synthetic_train_params

    all_exprs = list(
        set(
            [a['goal'] for a in human_processed_logs] + [a['goal'] for a in val_processed_logs] +
            all_params +
            [a['goal'] for a in synthetic_processed_logs] +
            [x for a in synthetic_processed_logs for x in a['thms_hard_negatives']]
        )

    ) + ['NO_PARAM']

    logging.info(f'{len(all_exprs)} unique expressions')
    logging.info('Generating data dictionary from expressions..')

    expr_dict = {expr: sexpression_to_graph(expr) for expr in tqdm(all_exprs)}

    human_processed_logs = list([{'goal': a['goal'], 'thms': a['thms'], 'tac_id': a['tac_id'],
                                      'thms_hard_negatives': a['thms_hard_negatives'], 'split': 'train',
                                      'source': 'human'} for a in
                                     human_processed_logs])

    synthetic_processed_logs = list([{'goal': a['goal'], 'thms': a['thms'], 'tac_id': a['tac_id'],
                                          'thms_hard_negatives': a['thms_hard_negatives'], 'split': 'train',
                                          'source': 'synthetic'} for a in
                                         synthetic_processed_logs])

    val_processed_logs = list([{'goal': a['goal'], 'thms': a['thms'], 'tac_id': a['tac_id'],
                                    'thms_hard_negatives': a['thms_hard_negatives'], 'split': 'val', 'source': 'human'}
                                   for a in val_processed_logs])

    if vocab_file:
        logging.info(f'Generating vocab from file {vocab_file}..')
        vocab = gen_vocab_dict(vocab_file)
        vocab['UNK'] = len(vocab)

    else:
        logging.info(f'Generating vocab from proof logs..')

        vocab_toks = set([token for expr in tqdm(expr_dict.values()) for token in expr['tokens']])
        vocab = {}
        vocab['NO_PARAM'] = 1

        for i, v in enumerate(vocab_toks):
            vocab[v] = i + 2

        vocab["UNK"] = len(vocab)
        vocab["("] = len(vocab)
        vocab[")"] = len(vocab)

    if source == 'mongodb':
        logging.info("Adding data to MongoDB")
        client = MongoClient()
        db = client[data_options['db']]
        expr_col = db['expression_graphs']
        split_col = db['split_data']
        vocab_col = db['vocab']
        thm_ls_col = db['train_thm_ls']

        logging.info("Adding full expression data..")

        for k, v in tqdm(expr_dict.items()):
            expr_col.insert_one({'_id': k, 'data': {'tokens': v['tokens'],
                                                    'edge_index': v['edge_index'],
                                                    'edge_attr': v['edge_attr'], }})

        split_col.insert_many(human_processed_logs)
        split_col.insert_many(synthetic_processed_logs)
        split_col.insert_many(val_processed_logs)

        vocab_col.insert_many([{'_id': k, 'index': v} for k, v in vocab.items()])
        thm_ls_col.insert_many([{'_id': x, 'source': 'human'} for x in list(set(human_train_params))])
        thm_ls_col.insert_many(
            [{'_id': x, 'source': 'synthetic'} for x in list(set(synthetic_train_params) - set(human_train_params))])


    elif source == 'directory':
        data = {'train_data': human_processed_logs + synthetic_processed_logs, 'val_data': val_processed_logs,
                'expr_dict': expr_dict, 'train_thm_ls': list(set(human_train_params)), 'vocab': vocab}

        save_dir = data_options['dir']
        os.makedirs(save_dir)
        torch.save(data, save_dir + '/data.pt')

    else:
        raise NotImplementedError

    logging.info('Done!')


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    human_train_logs = 'data/HOList/raw_data/hollightdata/final/proofs/human/train/prooflogs*'
    human_val_logs = 'data/HOList/raw_data/hollightdata/final/proofs/human/valid/prooflogs*'

    synthetic_train_logs = 'data/HOList/raw_data/hollightdata/final/proofs/synthetic/train/prooflogs*'

    all_train_logs = synthetic_train_logs + ',' + human_train_logs
    all_val_logs = human_val_logs

    config = {
        'tac_dir': 'experiments/HOList/config/holist/hollight_tactics.textpb',
        'theorem_dir': 'data/HOList/theorem_database_v1.1.textpb',
        'human_train_logs': human_train_logs,
        'val_logs': human_val_logs,
        'synthetic_train_logs': synthetic_train_logs,
        'vocab_file': None,
        'source': 'mongodb',
        'data_options': {'db': 'holist'},
    }

    prepare_data(config)

    source = config['source']
    data_options = config['data_options']

    client = MongoClient()
    db = client[data_options['db']]
    expr_col = db['expression_graphs']

    max_seq_len = 1024


    # uncomment below to add data for Directed SAT


    # def add_additional_data(item):
    #     try:
    #         expr_col.update_many({"_id": item["_id"]},
    #                              {"$set":
    #                                  {
    #                                      "data.attention_edge_index":
    #                                          get_directed_edge_index(len(item['tokens']),
    #                                                                  torch.LongTensor(
    #                                                                      item['edge_index'])).tolist(),
    #                                      "data.depth":
    #                                          get_depth_from_graph(len(item['tokens']),
    #                                                               torch.LongTensor(
    #                                                                   item['edge_index'])).tolist(),
    #
    #                                      'data.sequence': sexpression_to_polish(item["_id"])[:max_seq_len]
    #                                  }})
    #     except Exception as e:
    #         print(f"error {e}")
    #         pass
    #
    #
    # logging.info("Adding additional properties to expression database..")
    # items = []
    #
    # # can run the below to retry failed field insertions
    # # for item in tqdm(expr_col.find({'data.attention_edge_index': {'$exists': False}})):
    # #     items.append({'_id': item['_id'], 'tokens': item['data']['tokens'], 'edge_index': item['data']['edge_index']})
    #
    # for item in tqdm(expr_col.find({})):
    #     items.append({'_id': item['_id'], 'tokens': item['data']['tokens'], 'edge_index': item['data']['edge_index']})
    # print(f"num_items {len(items)}")
    #
    # pool = Pool(processes=os.cpu_count() // 2)
    # for _ in tqdm(pool.imap_unordered(add_additional_data, items), total=len(items)):
    #     pass
