"""Script for indexing the corpus using the retriever.
"""
import torch
import pickle
import argparse
from loguru import logger

from experiments.end_to_end.common import IndexedCorpus
from models.end_to_end.tactic_models.retrieval.model import PremiseRetriever


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Script for indexing a corpus given a trained retrieval model."
    )
    parser.add_argument("--ckpt_path", type=str, required=True)
    parser.add_argument("--corpus-path", type=str, required=True)
    parser.add_argument(
        "--output-path",
        type=str,
        required=True,
    )
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    logger.info(args)

    if not torch.cuda.is_available():
        logger.warning("Indexing the corpus using CPU can be very slow.")
        device = torch.device("cpu")
    else:
        device = torch.device("cuda")

    model = PremiseRetriever.load(args.ckpt_path, device, freeze=True)

    model.load_corpus(args.corpus_path)
    model.reindex_corpus(batch_size=args.batch_size)

    pickle.dump(
        IndexedCorpus(model.corpus, model.corpus_embeddings.cpu()),
        open(args.output_path, "wb"),
    )
    logger.info(f"Indexed corpus saved to {args.output_path}")


if __name__ == "__main__":
    main()
