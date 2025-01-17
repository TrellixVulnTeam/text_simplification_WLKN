from model.embedding import Embedding
from model.loss import sequence_loss
from model.metric import Metric
from model.optimizer import TransformerOptimizer
from util import constant

import tensorflow as tf
import numpy as np
import regex as re
from util.decode import decode, truncate_sents


class Graph():
    def __init__(self, data, is_train, model_config):
        self.model_config = model_config
        self.data = data
        self.is_train = is_train
        self.model_fn = None
        self.rand_unif_init = tf.random_uniform_initializer(-0,.08, 0.08)
        self.metric = Metric(self.model_config, self.data)

    def embedding_fn(self, inputs, embedding):
        if type(inputs) == list:
            if not inputs:
                return []
            else:
                return [tf.nn.embedding_lookup(embedding, inp) for inp in inputs]
        else:
            return tf.nn.embedding_lookup(embedding, inputs)

    def output_to_logit(self, prev_out, w, b):
        prev_logit = tf.add(tf.matmul(prev_out, tf.transpose(w)), b)
        return prev_logit

    def create_model_multigpu(self):
        losses = []
        grads = []
        ops = [tf.constant(0)]
        self.objs = []
        self.global_step = tf.train.get_or_create_global_step()
        optim = self.get_optim()

        fetch_data = None
        if self.model_config.fetch_mode == 'tf_example_dataset':
            fetch_data = self.data.get_data_sample()

        with tf.variable_scope(tf.get_variable_scope()) as scope:
            for gpu_id in range(self.model_config.num_gpus):
                with tf.device('/device:GPU:%d' % gpu_id):
                    with tf.name_scope('%s_%d' % ('gpu_scope', gpu_id)):
                        loss, obj = self.create_model(fetch_data=fetch_data)
                        if self.model_config.npad_mode == 'v1':
                            vars = tf.get_collection(
                                tf.GraphKeys.TRAINABLE_VARIABLES, scope='model/transformer_decoder/decoder/layer_5/npad/')
                            grad = optim.compute_gradients(loss, colocate_gradients_with_ops=True, var_list=vars)
                        elif self.model_config.npad_mode == 'static_seq':
                            vars = tf.get_collection(
                                tf.GraphKeys.TRAINABLE_VARIABLES,  scope='model/transformer_decoder/npad/')
                            grad = optim.compute_gradients(loss, colocate_gradients_with_ops=True, var_list=vars)
                        else:
                            grad = optim.compute_gradients(loss, colocate_gradients_with_ops=True)
                        tf.get_variable_scope().reuse_variables()
                        losses.append(loss)
                        grads.append(grad)
                        if 'rule' in self.model_config.memory and self.is_train:
                            ops.append(obj['mem_contexts'])
                            ops.append(obj['mem_outputs'])
                            ops.append(obj['mem_counter'])
                        self.objs.append(obj)

        with tf.variable_scope('optimization'):
                self.loss = tf.divide(tf.add_n(losses), self.model_config.num_gpus)
                self.perplexity = tf.exp(tf.reduce_mean(self.loss))

                if self.is_train:
                    avg_grad = self.average_gradients(grads)
                    grads = [g for (g,v) in avg_grad]
                    clipped_grads, _ = tf.clip_by_global_norm(grads, self.model_config.max_grad_norm)
                    if self.model_config.npad_mode == 'v1':
                        vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                                 scope='model/transformer_decoder/decoder/layer_5/npad/')
                    elif self.model_config.npad_mode == 'static_seq':
                        vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                                 scope='model/transformer_decoder/npad/')
                    else:
                        vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
                    self.train_op = optim.apply_gradients(zip(clipped_grads, vars), global_step=self.global_step)
                    self.increment_global_step = tf.assign_add(self.global_step, 1)

                self.saver = tf.train.Saver(write_version=tf.train.SaverDef.V2)
                self.ops = tf.tuple(ops)

    def create_model(self, fetch_data=None):
        with tf.variable_scope('variables'):
            sentence_simple_input_placeholder = []
            sentence_complex_input_placeholder = []
            if self.model_config.subword_vocab_size and self.model_config.seg_mode:
                sentence_simple_segment_input_placeholder = []
                sentence_complex_segment_input_placeholder = []

            obj = {}
            if fetch_data is not None and self.model_config.fetch_mode == 'tf_example_dataset':
                for t in tf.unstack(fetch_data['line_comp_ids'], axis=1):
                    sentence_complex_input_placeholder.append(t)
                for t in tf.unstack(fetch_data['line_simp_ids'], axis=1):
                    sentence_simple_input_placeholder.append(t)

                if self.model_config.subword_vocab_size and self.model_config.seg_mode:
                    for t in tf.unstack(fetch_data['line_comp_segids'], axis=1):
                        sentence_complex_segment_input_placeholder.append(t)
                    for t in tf.unstack(fetch_data['line_simp_segids'], axis=1):
                        sentence_simple_segment_input_placeholder.append(t)
                    obj['line_comp_segids'] = tf.stack(sentence_complex_segment_input_placeholder, axis=1)
                    obj['line_simp_segids'] = tf.stack(sentence_simple_segment_input_placeholder, axis=1)

                score = None
                if self.model_config.tune_style:
                    if self.is_train:
                        # In training, score are from fetch data
                        scores = []
                        if self.model_config.tune_style[0]:
                            ppdb_score = fetch_data['ppdb_score']
                            scores.append(ppdb_score)
                            print('Tune ppdb score!')
                            if 'plus' in self.model_config.tune_mode:
                                # to avoid most ppdb scores are 0
                                ppdb_score += 0.1
                        if self.model_config.tune_style[1]:
                            add_score = fetch_data['dsim_score']
                            scores.append(add_score)
                            print('Tune dsim_score score!')
                        if self.model_config.tune_style[2]:
                            add_score = fetch_data['add_score']
                            scores.append(add_score)
                            print('Tune add score!')
                        if self.model_config.tune_style[3]:
                            len_score = fetch_data['len_score']
                            scores.append(len_score)
                            print('Tune length score!')

                    else:
                        # In evaluating/predict, scores may be a  factor to multiply if in pred mode
                        #   or actual user provided score
                        # TODO(sanqiang): not used for now because not fech_data in eval
                        raise NotImplementedError('No tune style for training')
                        # ppdb_score = tf.constant(
                        #     self.model_config.tune_style[0], shape=[self.model_config.batch_size], dtype=tf.float32)
                        # add_score = tf.constant(
                        #     self.model_config.tune_style[1], shape=[self.model_config.batch_size], dtype=tf.float32)
                        # len_score = tf.constant(
                        #     self.model_config.tune_style[2], shape=[self.model_config.batch_size], dtype=tf.float32)

                    # Assemble scores
                    dimension_unit = int(self.model_config.dimension / len(scores))
                    dimension_runit = self.model_config.dimension - (len(scores)-1)*dimension_unit
                    for s_i, score in enumerate(scores):
                        if s_i < len(scores)-1:
                            scores[s_i] = tf.expand_dims(tf.tile(
                                tf.expand_dims(scores[s_i], axis=-1),
                                [1, dimension_unit]), axis=1)
                        else:
                            scores[s_i] = tf.expand_dims(tf.tile(
                                tf.expand_dims(scores[s_i], axis=-1),
                                [1, dimension_runit]), axis=1)
                    score = tf.concat(scores, axis=-1)
            else:
                for step in range(self.model_config.max_simple_sentence):
                    sentence_simple_input_placeholder.append(
                        tf.zeros(self.model_config.batch_size, tf.int32, name='simple_input'))

                for step in range(self.model_config.max_complex_sentence):
                    sentence_complex_input_placeholder.append(
                        tf.zeros(self.model_config.batch_size, tf.int32, name='complex_input'))

                if self.model_config.subword_vocab_size and self.model_config.seg_mode:
                    for step in range(self.model_config.max_simple_sentence):
                        sentence_simple_segment_input_placeholder.append(
                            tf.zeros(self.model_config.batch_size, tf.int32, name='simple_seg_input'))

                    for step in range(self.model_config.max_complex_sentence):
                        sentence_complex_segment_input_placeholder.append(
                            tf.zeros(self.model_config.batch_size, tf.int32, name='complex_seg_input'))

                    obj['line_comp_segids'] = tf.stack(sentence_complex_segment_input_placeholder, axis=1)
                    obj['line_simp_segids'] = tf.stack(sentence_simple_segment_input_placeholder, axis=1)

                score = None
                if self.model_config.tune_style:
                    if self.is_train:
                        raise NotImplementedError('No tune style for training')
                        #
                        # ppdb_score = tf.constant(
                        #     self.model_config.tune_style, shape=[self.model_config.batch_size], dtype=tf.float32)
                        # ppdb_score = tf.expand_dims(tf.tile(
                        #     tf.expand_dims(ppdb_score, axis=-1),
                        #     [1, self.model_config.dimension]), axis=1)
                    else:
                        scores = []
                        if self.model_config.tune_style:
                            if self.model_config.tune_style[0]:
                                ppdb_score = tf.constant(
                                    self.model_config.tune_style[0], shape=[self.model_config.batch_size], dtype=tf.float32)
                                scores.append(ppdb_score)
                                print('tune ppdb score')
                            if self.model_config.tune_style[1]:
                                dsim_score = tf.constant(
                                    self.model_config.tune_style[1], shape=[self.model_config.batch_size], dtype=tf.float32)
                                scores.append(dsim_score)
                                print('tune dsim score')
                            if self.model_config.tune_style[2]:
                                add_score = tf.constant(
                                    self.model_config.tune_style[2], shape=[self.model_config.batch_size], dtype=tf.float32)
                                scores.append(add_score)
                                print('tune add score')
                            if self.model_config.tune_style[3]:
                                len_score = tf.constant(
                                    self.model_config.tune_style[3], shape=[self.model_config.batch_size], dtype=tf.float32)
                                scores.append(len_score)
                                print('tune len score')
                    # Assemble scores
                    dimension_unit = int(self.model_config.dimension / len(scores))
                    dimension_runit = self.model_config.dimension - (len(scores)-1) * dimension_unit
                    for s_i, score in enumerate(scores):
                        if s_i < len(scores)-1:
                            scores[s_i] = tf.expand_dims(tf.tile(
                                tf.expand_dims(scores[s_i], axis=-1),
                                [1, dimension_unit]), axis=1)
                        else:
                            scores[s_i] = tf.expand_dims(tf.tile(
                                tf.expand_dims(scores[s_i], axis=-1),
                                [1, dimension_runit]), axis=1)
                    score = tf.concat(scores, axis=-1)

            # For self.model_config.tune_style:
            comp_features = {}
            comp_add_score = tf.zeros(self.model_config.batch_size, tf.float32, name='comp_add_score_input')
            comp_length = tf.zeros(self.model_config.batch_size, tf.float32, name='comp_length_input')
            comp_features['comp_add_score'] = comp_add_score
            comp_features['comp_length'] = comp_length

            sentence_idxs = tf.zeros(self.model_config.batch_size, tf.int32, name='sent_idx')

            self.embedding = Embedding(self.data.vocab_complex, self.data.vocab_simple, self.model_config)
            if self.model_config.bert_mode:
                emb_complex = None
            else:
                emb_complex = self.embedding.get_complex_embedding()
            if self.model_config.bert_mode and (
                    self.model_config.tie_embedding == 'all' or
                    self.model_config.tie_embedding == 'enc_dec'):
                emb_simple = None
            else:
                emb_simple = self.embedding.get_simple_embedding()

            if (self.is_train and self.model_config.pretrained_embedding):
                self.embed_complex_placeholder = tf.placeholder(
                    tf.float32, (self.data.vocab_complex.vocab_size(), self.model_config.dimension),
                    'complex_emb')
                self.replace_emb_complex = emb_complex.assign(self.embed_complex_placeholder)

                self.embed_simple_placeholder = tf.placeholder(
                    tf.float32, (self.data.vocab_simple.vocab_size(), self.model_config.dimension),
                    'simple_emb')
                self.replace_emb_simple = emb_simple.assign(self.embed_simple_placeholder)

            if self.model_config.bert_mode and (
                    self.model_config.tie_embedding == 'all' or
                    self.model_config.tie_embedding == 'dec_out'):
                w = None
            else:
                w = self.embedding.get_w()
            b = self.embedding.get_b()

            mem_contexts, mem_outputs, mem_counter = None, None, None
            rule_id_input_placeholder, rule_target_input_placeholder = [], []
            if 'rule' in self.model_config.memory:
                with tf.device('/cpu:0'):
                    context_size = 0
                    if self.model_config.framework == 'transformer':
                        context_size = 1
                    elif self.model_config.framework == 'seq2seq':
                        context_size = 2
                    mem_contexts = tf.get_variable(
                        'mem_contexts',
                        initializer=tf.constant(0, dtype=tf.float32, shape=(
                            self.data.vocab_rule.get_rule_size(),
                            self.model_config.max_target_rule_sublen,
                            self.model_config.dimension * context_size)),
                        trainable=False, dtype=tf.float32)
                    mem_outputs = tf.get_variable(
                        'mem_outputs',
                        initializer=tf.constant(0, dtype=tf.float32, shape=(
                            self.data.vocab_rule.get_rule_size(),
                            self.model_config.max_target_rule_sublen,
                            self.model_config.dimension)),
                        trainable=False, dtype=tf.float32)
                    mem_counter = tf.get_variable(
                        'mem_counter',
                        initializer=tf.constant(0, dtype=tf.int32, shape=(self.data.vocab_rule.get_rule_size(), 1)),
                        trainable=False, dtype=tf.int32)

            if 'direct' in self.model_config.memory or 'rule' in self.model_config.memory:
                if fetch_data is not None and self.model_config.fetch_mode == 'tf_example_dataset':
                    for t in tf.unstack(fetch_data['rule_id'], axis=1):
                        rule_id_input_placeholder.append(t)
                    for t in tf.unstack(fetch_data['rule_target'], axis=1):
                        rule_target_input_placeholder.append(t)
                else:
                    for step in range(self.model_config.max_cand_rules):
                        rule_id_input_placeholder.append(
                            tf.zeros(self.model_config.batch_size, tf.int32, name='rule_id_input'))

                    for step in range(self.model_config.max_cand_rules):
                        if 'direct' in self.model_config.memory:
                            rule_target_input_placeholder.append(
                                tf.zeros(self.model_config.batch_size, tf.int32, name='rule_target_input'))
                        elif 'rule' in self.model_config.memory:
                            rule_target_input_placeholder.append(
                                tf.zeros(self.model_config.batch_size, tf.string, name='rule_target_input'))

        with tf.variable_scope('model'):
            output = self.model_fn(sentence_complex_input_placeholder, emb_complex,
                                   sentence_simple_input_placeholder, emb_simple,
                                   w, b, rule_id_input_placeholder, rule_target_input_placeholder,
                                   mem_contexts, mem_outputs,
                                   self.global_step, score, comp_features, obj)

            encoder_embs, final_outputs = None, None
            if self.model_config.replace_unk_by_emb:
                encoder_embs = tf.stack(output.encoder_embed_inputs_list, axis=1)

            if output.decoder_outputs_list is not None:
                if type(output.decoder_outputs_list) == list:
                    decoder_outputs_list = output.decoder_outputs_list
                    decoder_outputs = tf.stack(decoder_outputs_list, axis=1)
                else:
                    decoder_outputs = output.decoder_outputs_list

            if output.final_outputs_list is not None:
                if type(output.final_outputs_list) == list:
                    final_outputs_list = output.final_outputs_list
                    final_outputs = tf.stack(final_outputs_list, axis=1)
                else:
                    final_outputs = output.final_outputs_list

            attn_distr = None
            if self.model_config.replace_unk_by_attn:
                attn_distr = output.attn_distr_list

            if not self.is_train:
                # in beam search, it directly provide decoder target list
                decoder_target = tf.stack(output.decoder_target_list, axis=1)
                loss = tf.reduce_mean(output.decoder_score)
                obj = {
                    'sentence_idxs': sentence_idxs,
                    'sentence_simple_input_placeholder': sentence_simple_input_placeholder,
                    'sentence_complex_input_placeholder': sentence_complex_input_placeholder,
                    'decoder_target_list': decoder_target,
                    'final_outputs':final_outputs,
                    'encoder_embs':encoder_embs,
                    'attn_distr':attn_distr
                }
                if self.model_config.subword_vocab_size and self.model_config.seg_mode:
                    obj['sentence_complex_segment_input_placeholder'] = sentence_complex_segment_input_placeholder
                    obj['sentence_simple_segment_input_placeholder'] = sentence_simple_segment_input_placeholder
                if 'rule' in self.model_config.memory or 'direct' in self.model_config.memory:
                    obj['rule_id_input_placeholder'] = rule_id_input_placeholder
                    obj['rule_target_input_placeholder'] = rule_target_input_placeholder
                if self.model_config.tune_style:
                    obj['comp_features'] = comp_features
                return loss, obj
            else:
                # Memory Populate
                if 'rule' in self.model_config.memory:
                    # Update Memory through python injection
                    def update_memory(
                            mem_contexts_tmp, mem_outputs_tmp, mem_counter_tmp,
                            decoder_targets, decoder_outputs, contexts,
                            rule_target_input_placeholder, rule_id_input_placeholder,
                            global_step, encoder_outputs):
                        def _seq_contain(arr, tar):
                            j = 0
                            for i in range(len(arr)):
                                if arr[i] == tar[j]:
                                    j += 1
                                    if j == len(tar):
                                        return i - len(tar) + 1
                                else:
                                    j = 0
                            return -1

                        # if 'stopgrad' in self.model_config.rl_configs and global_step % 2 != 0:
                        #     return mem_contexts_tmp, mem_outputs_tmp, mem_counter_tmp
                        # if global_step <= self.model_config.memory_prepare_step:
                        #     return mem_contexts_tmp, mem_outputs_tmp, mem_counter_tmp

                        batch_size = np.shape(rule_target_input_placeholder)[0]
                        max_rules = np.shape(rule_target_input_placeholder)[1]
                        decoder_targets_str = [' '.join(sent) for sent in truncate_sents(decode(
                            decoder_targets, self.data.vocab_simple,
                            self.model_config.subword_vocab_size>0 or 'bert_token' in self.model_config.bert_mode))]
                        for batch_id in range(batch_size):
                            cur_decoder_targets = decoder_targets[batch_id, :]
                            cur_decoder_targets_str = decoder_targets_str[batch_id]

                            cur_decoder_outputs = decoder_outputs[batch_id, :]
                            cur_contexts = contexts[batch_id, :]

                            cur_rule_target_input_placeholder = rule_target_input_placeholder[batch_id, :]
                            cur_rule_target_input_placeholder = [tmp.decode("utf-8").strip('\x00')
                                                                 for tmp in cur_rule_target_input_placeholder
                                                                 if not tmp.decode("utf-8").strip().startswith(constant.SYMBOL_PAD)]
                            cur_rule_id_input_placeholder = rule_id_input_placeholder[batch_id, :]

                            # Build the valid mapper from rule id => target words ids
                            rule_mapper = {}
                            for step in range(len(cur_rule_target_input_placeholder)):
                                rule_target_str = cur_rule_target_input_placeholder[step]
                                if rule_target_str == constant.SYMBOL_PAD:
                                    continue
                                rule_id = cur_rule_id_input_placeholder[step]
                                if rule_id != 0 and re.search(r'\b%s\b' % rule_target_str, cur_decoder_targets_str): # decoder_target_str in cur_decoder_targets_str:
                                    decoder_target_wids = self.data.vocab_simple.encode(rule_target_str)
                                    dec_s_idx = _seq_contain(cur_decoder_targets, decoder_target_wids)
                                    if dec_s_idx != -1:
                                        print('rule_target_str:%s' % rule_target_str)
                                        print('cur_decoder_targets_str:%s' % cur_decoder_targets_str)
                                        print('cur_decoder_targets:%s' % cur_decoder_targets)
                                        print('decoder_target_wids:%s' % decoder_target_wids)
                                    rule_mapper[rule_id] = list(range(dec_s_idx, dec_s_idx+len(decoder_target_wids)))

                            for rule_id in rule_mapper:
                                dec_idxs = rule_mapper[rule_id]

                                for idx, dec_idx in enumerate(dec_idxs):
                                    if mem_counter_tmp[rule_id, 0] == 0:
                                        mem_contexts_tmp[rule_id, idx, :] = cur_contexts[dec_idx, :]
                                        mem_outputs_tmp[rule_id, idx, :] = cur_decoder_outputs[dec_idx, :]
                                    else:
                                        mem_contexts_tmp[rule_id, idx, :] = (cur_contexts[dec_idx, :] + mem_contexts_tmp[rule_id, idx, :]) / 2
                                        mem_outputs_tmp[rule_id, idx, :] = (cur_decoder_outputs[dec_idx, :] + mem_outputs_tmp[rule_id, idx, :]) / 2

                                mem_counter_tmp[rule_id, 0] += 1

                        return mem_contexts_tmp, mem_outputs_tmp, mem_counter_tmp

                    mem_output_input = None
                    if 'mofinal' in self.model_config.memory_config:
                        mem_output_input = final_outputs
                    # elif 'modecode' in self.model_config.memory_config:
                    #     mem_output_input = decoder_outputs
                    # elif 'moemb' in self.model_config.memory_config:
                    #     mem_output_input = tf.stack(
                    #         self.embedding_fn(sentence_simple_input_placeholder, emb_simple),
                    #         axis=1)

                    mem_contexts, mem_outputs, mem_counter = tf.py_func(update_memory,
                                                                        [mem_contexts, mem_outputs, mem_counter,
                                                                         tf.stack(output.decoder_target_list, axis=1), mem_output_input,
                                                                         output.contexts,
                                                                         tf.stack(rule_target_input_placeholder, axis=1),
                                                                         tf.stack(rule_id_input_placeholder, axis=1),
                                                                         self.global_step,
                                                                         output.encoder_outputs],
                                                                        [tf.float32, tf.float32, tf.int32],
                                                                        stateful=False, name='update_memory')

                #Loss and corresponding prior/mask
                decode_word_weight_list = [tf.to_float(tf.not_equal(d, self.data.vocab_simple.encode(constant.SYMBOL_PAD)))
                     for d in output.gt_target_list]
                decode_word_weight = tf.stack(decode_word_weight_list, axis=1)

                gt_target = tf.stack(output.gt_target_list, axis=1)

                def self_critical_loss():
                    # For minimize the negative log of probabilities
                    rewards = tf.py_func(self.metric.self_crititcal_reward,
                                         [sentence_idxs,
                                          tf.stack(output.sample_target_list, axis=-1),
                                          tf.stack(output.decoder_target_list, axis=-1),
                                          tf.stack(sentence_simple_input_placeholder, axis=-1),
                                          tf.stack(sentence_complex_input_placeholder, axis=-1),
                                          tf.ones((1, 1)),
                                          # tf.stack(rule_target_input_placeholder, axis=1)
                                          ],
                                         tf.float32, stateful=False, name='reward')
                    rewards.set_shape((self.model_config.batch_size, self.model_config.max_simple_sentence))
                    rewards = tf.unstack(rewards, axis=1)

                    weighted_probs_list = [rewards[i] * decode_word_weight_list[i] * -output.sample_logit_list[i]
                                      for i in range(len(decode_word_weight_list))]
                    total_size = tf.reduce_sum(decode_word_weight_list)
                    total_size += 1e-12
                    weighted_probs = tf.reduce_sum(weighted_probs_list) / total_size
                    loss = weighted_probs
                    return loss

                def teacherforce_critical_loss():
                    losses = []
                    for step in range(self.model_config.max_simple_sentence):
                        logit = output.decoder_logit_list[step]
                        greedy_target_unit = tf.stop_gradient(tf.argmax(logit, axis=1))
                        if self.model_config.train_mode == 'teachercriticalv2':
                            sampled_target_unit, reward = tf.py_func(self.metric.self_crititcal_reward_unitv2,
                                                [sentence_idxs, step,
                                                 greedy_target_unit,
                                                 tf.stack(sentence_simple_input_placeholder, axis=-1),
                                                 tf.stack(sentence_complex_input_placeholder, axis=-1),
                                                 self.global_step
                                                 ],
                                                [tf.int32, tf.float32], stateful=False, name='reward')
                            reward.set_shape((self.model_config.batch_size,))
                            sampled_target_unit.set_shape((self.model_config.batch_size,))
                        elif self.model_config.train_mode == 'teachercritical':
                            sampled_target_unit = tf.cast(tf.squeeze(tf.multinomial(logit, 1), axis=1), tf.int32)
                            sampled_target_unit, reward = tf.py_func(self.metric.self_crititcal_reward_unit,
                                                 [sentence_idxs, step,
                                                  sampled_target_unit, greedy_target_unit,
                                                  tf.stack(sentence_simple_input_placeholder, axis=-1),
                                                  tf.stack(sentence_complex_input_placeholder, axis=-1),
                                                  self.global_step,
                                                  ],
                                                  [tf.int32, tf.float32], stateful=False, name='reward')
                            reward.set_shape((self.model_config.batch_size, ))
                            sampled_target_unit.set_shape((self.model_config.batch_size,))
                        indices = tf.stack(
                            [tf.range(0, self.model_config.batch_size, dtype=tf.int32),
                             tf.squeeze(sampled_target_unit)],
                            axis=-1)
                        logit_unit = tf.gather_nd(tf.nn.softmax(logit, axis=1), indices)
                        decode_word_weight = decode_word_weight_list[step]
                        losses.append(-logit_unit * reward * decode_word_weight)
                    loss = tf.add_n(losses)
                    return loss

                def teacherforce_loss():
                    if self.model_config.number_samples > 0:
                        loss_fn = tf.nn.sampled_softmax_loss
                    else:
                        loss_fn = None
                    loss = sequence_loss(logits=tf.stack(output.decoder_logit_list, axis=1),
                                         targets=gt_target,
                                         weights=decode_word_weight,
                                         # softmax_loss_function=loss_fn,
                                         # w=w,
                                         # b=b,
                                         # decoder_outputs=decoder_outputs,
                                         # number_samples=self.model_config.number_samples
                                         )
                    return loss

                if self.model_config.train_mode == 'dynamic_self-critical':
                    loss = self_critical_loss()
                    # loss = tf.cond(
                    #     tf.greater(self.global_step, 50000),
                    #     # tf.logical_and(tf.greater(self.global_step, 100000), tf.equal(tf.mod(self.global_step, 2), 0)),
                    #     lambda : self_critical_loss(),
                    #     lambda : teacherforce_loss())
                elif self.model_config.train_mode == 'teachercritical' or self.model_config.train_mode == 'teachercriticalv2':
                    loss = tf.cond(
                        tf.equal(tf.mod(self.global_step, 2), 0),
                        lambda : teacherforce_loss(),
                        lambda : teacherforce_critical_loss())

                    # loss = teacherforce_critical_loss()
                else:
                    loss = teacherforce_loss()

                if self.model_config.architecture == 'ut2t':
                    assert 'extra_encoder_loss' in output.obj_tensors and 'extra_decoder_loss' in output.obj_tensors
                    loss += output.obj_tensors['extra_encoder_loss']
                    loss += output.obj_tensors['extra_decoder_loss']
                    print('Use U T2T with ACT')

                self.loss_style = tf.constant(0.0, dtype=tf.float32)
                if output.pred_score_tuple is not None and 'pred' in self.model_config.tune_mode:
                    print('Create loss for predicting style')
                    ppdb_pred_score, add_pred_score, len_pred_score = output.pred_score_tuple
                    # ppdb_pred_score = tf.Print(ppdb_pred_score, [ppdb_pred_score, fetch_data['ppdb_score']],
                    #                            message='ppdb_pred_score:', first_n=-1, summarize=100)
                    # add_pred_score = tf.Print(add_pred_score, [add_pred_score, fetch_data['add_score']],
                    #                            message='add_pred_score:', first_n=-1, summarize=100)
                    # len_pred_score = tf.Print(len_pred_score, [len_pred_score, fetch_data['len_score']],
                    #                            message='len_pred_score:', first_n=-1, summarize=100)
                    # loss = tf.Print(loss, [loss], message='loss before:', summarize=100)
                    self.loss_style += tf.losses.absolute_difference(ppdb_pred_score, fetch_data['ppdb_score'])
                    self.loss_style += tf.losses.absolute_difference(add_pred_score, fetch_data['add_score'])
                    self.loss_style += tf.losses.absolute_difference(len_pred_score, fetch_data['len_score'])
                    loss += self.loss_style
                    # loss = tf.Print(loss, [loss], message='loss after:', summarize=100)

                obj = {
                    'decoder_target_list': output.decoder_target_list,
                    'sentence_idxs': sentence_idxs,
                    'sentence_simple_input_placeholder': sentence_simple_input_placeholder,
                    'sentence_complex_input_placeholder': sentence_complex_input_placeholder,
                }
                self.logits = output.decoder_logit_list
                if 'rule' in self.model_config.memory:
                    obj['rule_id_input_placeholder'] = rule_id_input_placeholder
                    obj['rule_target_input_placeholder'] = rule_target_input_placeholder
                    # obj['rule_pair_input_placeholder'] = rule_pair_input_placeholder
                    obj['mem_contexts'] = mem_contexts
                    obj['mem_outputs'] = mem_outputs
                    obj['mem_counter'] = mem_counter
                return loss, obj

    def get_optim(self):
        learning_rate = tf.constant(self.model_config.learning_rate)

        if self.model_config.optimizer == 'adagrad':
            opt = tf.train.AdagradOptimizer(learning_rate)
        # Adam need lower learning rate
        elif self.model_config.optimizer == 'adam':
            opt = tf.train.AdamOptimizer(learning_rate)
        elif self.model_config.optimizer == 'lazy_adam':
            if not hasattr(self, 'hparams'):
                # In case not using Transformer model
                from tensor2tensor.models import transformer
                self.hparams = transformer.transformer_base()
            opt = tf.contrib.opt.LazyAdamOptimizer(
                self.hparams.learning_rate / 100.0,
                beta1=self.hparams.optimizer_adam_beta1,
                beta2=self.hparams.optimizer_adam_beta2,
                epsilon=self.hparams.optimizer_adam_epsilon)
        elif self.model_config.optimizer == 'adadelta':
            opt = tf.train.AdadeltaOptimizer(learning_rate)
        elif self.model_config.optimizer == 'sgd':
            opt = tf.train.GradientDescentOptimizer(learning_rate)
        else:
            raise Exception('Not Implemented Optimizer!')

        # if self.model_config.max_grad_staleness > 0:
        #     opt = tf.contrib.opt.DropStaleGradientOptimizer(opt, self.model_config.max_grad_staleness)

        return opt

    # Got from https://github.com/tensorflow/models/blob/master/tutorials/image/cifar10/cifar10_multi_gpu_train.py#L101
    def average_gradients(self, tower_grads):
        """Calculate the average gradient for each shared variable across all towers.
        Note that this function provides a synchronization point across all towers.
        Args:
          tower_grads: List of lists of (gradient, variable) tuples. The outer list
            is over individual gradients. The inner list is over the gradient
            calculation for each tower.
        Returns:
           List of pairs of (gradient, variable) where the gradient has been averaged
           across all towers.
        """
        average_grads = []
        for grad_and_vars in zip(*tower_grads):
            # Note that each grad_and_vars looks like the following:
            #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
            grads = []
            for g, _ in grad_and_vars:
                # Add 0 dimension to the gradients to represent the tower.
                if g is None:
                    print('Useless tensors:%s' % grad_and_vars)
                expanded_g = tf.expand_dims(g, 0)

                # Append on a 'tower' dimension which we will average over below.
                grads.append(expanded_g)

            # Average over the 'tower' dimension.
            grad = tf.concat(axis=0, values=grads)
            grad = tf.reduce_mean(grad, 0)

            # Keep in mind that the Variables are redundant because they are shared
            # across towers. So .. we will just return the first tower's pointer to
            # the Variable.
            v = grad_and_vars[0][1]
            grad_and_var = (grad, v)
            average_grads.append(grad_and_var)
        return average_grads

class ModelOutput:
    def __init__(self, decoder_outputs_list=None, decoder_logit_list=None, decoder_target_list=None,
                 decoder_score=None, gt_target_list=None, encoder_embed_inputs_list=None, encoder_outputs=None,
                 contexts=None, final_outputs_list=None, sample_target_list=None, sample_logit_list=None, attn_distr_list=None,
                 pred_score_tuple=None, obj_tensors=None):
        self._decoder_outputs_list = decoder_outputs_list
        self._decoder_logit_list = decoder_logit_list
        self._decoder_target_list = decoder_target_list
        self._decoder_score = decoder_score
        self._gt_target_list = gt_target_list
        self._encoder_embed_inputs_list = encoder_embed_inputs_list
        self._encoder_outputs = encoder_outputs
        self._contexts = contexts
        self._final_outputs_list = final_outputs_list
        self._sample_target_list = sample_target_list
        self._sample_logit_list = sample_logit_list
        self._attn_distr_list = attn_distr_list
        self._pred_score_tuple = pred_score_tuple
        self._obj_tensors = obj_tensors

    @property
    def encoder_outputs(self):
        return self._encoder_outputs

    @property
    def encoder_embed_inputs_list(self):
        """The final embedding input before model."""
        return self._encoder_embed_inputs_list

    @property
    def decoder_outputs_list(self):
        return self._decoder_outputs_list

    @property
    def final_outputs_list(self):
        return self._final_outputs_list

    @property
    def decoder_logit_list(self):
        return self._decoder_logit_list

    @property
    def decoder_target_list(self):
        return self._decoder_target_list

    @property
    def contexts(self):
        return self._contexts

    @property
    def decoder_score(self):
        return self._decoder_score

    @property
    def gt_target_list(self):
        return self._gt_target_list

    @property
    def sample_target_list(self):
        return self._sample_target_list

    @property
    def sample_logit_list(self):
        return self._sample_logit_list

    @property
    def attn_distr_list(self):
        return self._attn_distr_list

    @property
    def pred_score_tuple(self):
        return self._pred_score_tuple

    @property
    def obj_tensors(self):
        return self._obj_tensors
