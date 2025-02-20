import logging
from typing import Text

from data.HOList.utils import sexpression_graphs
from data.HOList.utils.sexpression_graphs import SExpressionGraph

def sexpression_to_polish(sexpression_text):
    sexpression = SExpressionGraph()
    sexpression.add_sexp(sexpression_text)
    out = []

    def process_node(node):
        if len(sexpression.get_children(node)) == 0:
            out.append(node)


        for i, child in enumerate(sexpression.get_children(node)):
            if i == 0:
                node_text = sexpression.to_text(child)
                # out.append(node_text)

                if node_text[0] == '(':
                    node_text = 'APPLY_TOK'
                    out.append('@')
                    out.append(node_text)
                    process_node(sexpression.to_text(child))
                    continue
                else:
                    for i in range(len(sexpression.get_children(node)) - 1):
                        out.append('@')
                    out.append(node_text)
                    continue

            process_node(sexpression.to_text(child))

    process_node(sexpression.to_text(sexpression.roots()[0]))
    return out

def sexpression_to_graph(sexpression_txt: Text):
    sexpression = SExpressionGraph(sexpression_txt)

    edges = []
    node_to_tok = {}

    def process_sexpression_graph(node, depth):
        node_id = sexpression_graphs.to_node_id(node)

        if len(sexpression.get_children(node)) == 0:
            if node_id not in node_to_tok:
                node_to_tok[node_id] = node

            assert sexpression.is_leaf_node(node_id)

        for i, child in enumerate(sexpression.get_children(node)):
            if i == 0:
                node_text = sexpression.to_text(child)

                if node_text[0] == '(':
                    node_text = 'APPLY_TOK'
                    node_to_tok[node_id] = node_text
                    edges.append((node_id, child, i))
                    process_sexpression_graph(sexpression.to_text(child), depth + 1)
                    continue
                else:
                    node_to_tok[node_id] = node_text
                    continue

            edges.append((node_id, child, i))
            process_sexpression_graph(sexpression.to_text(child), depth + 1)

    if len(sexpression.roots()) > 1:
        logging.warning(
            f"Multiple roots for {sexpression_txt}: {[sexpression.to_text(sexpression.roots()[i]) for i in sexpression.roots()]}")

    process_sexpression_graph(sexpression.to_text(sexpression.roots()[0]), 0)

    edges = set(edges)
    senders = [a[0] for a in edges]
    receivers = [a[1] for a in edges]
    edge_attr = [a[2] for a in edges]
    if len(edges) > 0:

        all_nodes = list(set(senders + receivers))
        senders = [all_nodes.index(i) for i in senders]
        receivers = [all_nodes.index(i) for i in receivers]

        node_to_tok_ = {}
        for k, v in node_to_tok.items():
            node_to_tok_[all_nodes.index(k)] = v

        assert len(node_to_tok_) == len(all_nodes)

        tok_list = [0 for _ in range(len(all_nodes))]

        for k, v in node_to_tok_.items():
            tok_list[k] = v

    else:
        tok_list = ["UNK"]

    return {'tokens': tok_list, 'edge_index': [senders, receivers], 'edge_attr': edge_attr}
