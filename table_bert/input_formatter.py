from math import ceil
from random import choice, shuffle, sample, random
from typing import List, Callable

from pytorch_pretrained_bert import BertTokenizer

from table_bert.table_bert import MAX_BERT_INPUT_LENGTH
from table_bert.config import TableBertConfig
from table_bert.dataset import Example
from table_bert.table import Column, Table


class TableBertBertInputFormatter(object):
    def __init__(self, config: TableBertConfig):
        self.config = config
        self.tokenizer = BertTokenizer.from_pretrained(
            config.base_model_name, do_lower_case=config.do_lower_case)

        self.vocab_list = list(self.tokenizer.vocab.keys())


class VanillaTableBertInputFormatter(TableBertBertInputFormatter):
    def get_cell_input(
        self,
        column: Column,
        cell_value: List[str],
        token_offset: int
    ):
        input = []
        span_map = {
            'first_token': (token_offset, token_offset + 1)
        }

        for token in self.config.cell_input_template:
            start_token_abs_position = len(input) + token_offset
            if token == 'column':
                span_map['column_name'] = (start_token_abs_position,
                                           start_token_abs_position + len(column.name_tokens))
                input.extend(column.name_tokens)
            elif token == 'value':
                span_map['value'] = (start_token_abs_position,
                                     start_token_abs_position + len(cell_value))
                input.extend(cell_value)
            elif token == 'type':
                span_map['type'] = (start_token_abs_position,
                                    start_token_abs_position + 1)
                input.append(column.type)
            else:
                span_map.setdefault('other_tokens', []).append(start_token_abs_position)
                input.append(token)

        span_map['whole_span'] = (token_offset, token_offset + len(input))

        return input, span_map

    def get_input(self, context: List[str], table: Table):
        if self.config.context_first:
            table_tokens_start_idx = len(context) + 2  # account for [CLS] and [SEP]
            # account for [CLS] and [SEP], and the ending [SEP]
            max_table_token_length = MAX_BERT_INPUT_LENGTH - len(context) - 2 - 1
        else:
            table_tokens_start_idx = 1  # account for starting [CLS]
            # account for [CLS] and [SEP], and the ending [SEP]
            max_table_token_length = MAX_BERT_INPUT_LENGTH - len(context) - 2 - 1

        # generate table tokens
        row_input_tokens = []
        column_token_span_maps = {}
        column_start_idx = table_tokens_start_idx

        for col_id, column in enumerate(table.header):
            value_tokens = column.sample_value_tokens
            truncated_value_tokens = value_tokens[:self.config.max_cell_len]

            column_input_tokens, token_span_map = self.get_cell_input(
                column,
                truncated_value_tokens,
                token_offset=column_start_idx
            )
            column_input_tokens.append(self.config.column_delimiter)

            if len(row_input_tokens) + len(column_input_tokens) > max_table_token_length:
                break

            row_input_tokens.extend(column_input_tokens)
            column_start_idx = column_start_idx + len(column_input_tokens)
            column_token_span_maps[column.name] = token_span_map

        if row_input_tokens[-1] == self.config.column_delimiter:
            del row_input_tokens[-1]

        if self.config.context_first:
            sequence = ['[CLS]'] + context + ['[SEP]'] + row_input_tokens + ['[SEP]']
            segment_ids = [0] * (len(context) + 2) + [1] * (len(row_input_tokens) + 1)
            context_token_indices = list(range(0, 1 + len(context)))
        else:
            sequence = ['[CLS]'] + row_input_tokens + ['[SEP]'] + context + ['[SEP]']
            segment_ids = [0] * (len(row_input_tokens) + 2) + [1] * (len(context) + 1)
            context_token_indices = list(range(len(row_input_tokens) + 1, len(row_input_tokens) + 1 + 1 + len(context) + 1))

        instance = {
            'tokens': sequence,
            'segment_ids': segment_ids,
            'column_spans': column_token_span_maps,
            'context_length': 1 + len(context),  # [CLS]/[SEP] + input question
            'context_token_indices': context_token_indices
        }

        return instance

    def get_pretraining_instances_from_example(
        self, example: Example,
        context_sampler: Callable
    ):
        instances = []
        context_iter = context_sampler(
            example, self.config.max_context_len, context_sample_strategy=self.config.context_sample_strategy)

        for context in context_iter:
            # row_num = len(example.column_data)
            # sampled_row_id = choice(list(range(row_num)))

            for col_idx, column in enumerate(example.header):
                col_values = example.column_data[col_idx]
                col_values = [val for val in col_values if val is not None and len(val) > 0]

                sampled_value = choice(col_values)

                # print('chosen value', sampled_value)
                sampled_value_tokens = self.tokenizer.tokenize(sampled_value)
                column.sample_value_tokens = sampled_value_tokens

            instance = self.create_pretraining_instance(context, example.header)
            instance['source'] = example.source

            instances.append(instance)

        return instances

    def create_pretraining_instance(self, context, header):
        table = Table('fake_table', header)
        input_instance = self.get_input(context, table)
        column_spans = input_instance['column_spans']

        column_candidate_indices = [
            (
                    list(range(*span['column_name'])) +
                    list(range(*span['type'])) +
                    (
                        span['other_tokens']
                        if random() < 0.01
                        else []
                    )
            )
            for col_name, span
            in column_spans.items()
        ]

        context_candidate_indices = (
            input_instance['context_token_indices'][1:]
            if self.config.context_first
            else input_instance['context_token_indices'][:-1]
        )

        masked_sequence, masked_lm_positions, masked_lm_labels, info = self.create_masked_lm_predictions(
            input_instance['tokens'], context_candidate_indices, column_candidate_indices
        )

        info['num_columns'] = len(header)

        instance = {
            "tokens": masked_sequence,
            "token_ids": self.tokenizer.convert_tokens_to_ids(masked_sequence),
            "segment_a_length": sum(1 for x in input_instance['segment_ids'] if x == 0),
            "masked_lm_positions": masked_lm_positions,
            "masked_lm_labels": masked_lm_labels,
            "masked_lm_label_ids": self.tokenizer.convert_tokens_to_ids(masked_lm_labels),
            "info": info
        }

        return instance

    def create_masked_lm_predictions(
        self,
        tokens, context_indices, column_indices
    ):
        table_mask_strategy = self.config.table_mask_strategy

        info = dict()
        info['num_maskable_column_tokens'] = sum(len(token_ids) for token_ids in column_indices)

        if table_mask_strategy == 'column_token':
            column_indices = [i for l in column_indices for i in l]
            num_column_tokens_to_mask = min(self.config.max_predictions_per_seq,
                                            max(2, int(len(column_indices) * self.config.masked_column_prob)))
            shuffle(column_indices)
            masked_column_token_indices = sorted(sample(column_indices, num_column_tokens_to_mask))
        elif table_mask_strategy == 'column':
            num_maskable_columns = len(column_indices)
            num_column_to_mask = max(1, ceil(num_maskable_columns * self.config.masked_column_prob))
            columns_to_mask = sorted(sample(list(range(num_maskable_columns)), num_column_to_mask))
            shuffle(columns_to_mask)
            num_column_tokens_to_mask = sum(len(column_indices[i]) for i in columns_to_mask)
            masked_column_token_indices = [idx for col in columns_to_mask for idx in column_indices[col]]

            info['num_masked_columns'] = num_column_to_mask
        else:
            raise RuntimeError('unknown mode!')

        max_context_token_to_mask = self.config.max_predictions_per_seq - num_column_tokens_to_mask
        num_context_tokens_to_mask = min(max_context_token_to_mask,
                                         max(1, int(len(context_indices) * self.config.masked_context_prob)))

        if num_context_tokens_to_mask > 0:
            # if num_context_tokens_to_mask < 0 or num_context_tokens_to_mask > len(context_indices):
            #     for col_id in columns_to_mask:
            #         print([tokens[i] for i in column_indices[col_id]])
            #     print(num_context_tokens_to_mask, num_column_tokens_to_mask)
            shuffle(context_indices)
            masked_context_token_indices = sorted(sample(context_indices, num_context_tokens_to_mask))
            masked_indices = sorted(masked_context_token_indices + masked_column_token_indices)
        else:
            masked_indices = masked_column_token_indices

        masked_token_labels = []

        for index in masked_indices:
            # 80% of the time, replace with [MASK]
            if random() < 0.8:
                masked_token = "[MASK]"
            else:
                # 10% of the time, keep original
                if random() < 0.5:
                    masked_token = tokens[index]
                # 10% of the time, replace with random word
                else:
                    masked_token = choice(self.vocab_list)
            masked_token_labels.append(tokens[index])
            # Once we've saved the true label for that token, we can overwrite it with the masked version
            tokens[index] = masked_token

        info.update({
            'num_column_tokens_to_mask': num_column_tokens_to_mask,
            'num_context_tokens_to_mask': num_context_tokens_to_mask,
        })

        return tokens, masked_indices, masked_token_labels, info