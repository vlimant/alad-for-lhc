import sys
import time
import logging

import tensorflow as tf
import sklearn
import numpy as np
from scipy.stats import binned_statistic

from core.skeleton import *

logger = logging.getLogger("ALAD")


class ALAD(AbstractAnomalyDetector):
    def __init__(self, config, sess):
        self.config = config
        self.sess = sess

        # Parameters
        starting_lr = config.learning_rate
        batch_size = config.batch_size
        latent_dim = config.latent_dim
        ema_decay = config.ema_decay

        global_step = tf.Variable(0, name='global_step', trainable=False)

        # Placeholders
        x_pl = tf.placeholder(tf.float32, shape=[None, config.input_dim],
                              name="input_x")
        z_pl = tf.placeholder(tf.float32, shape=[None, latent_dim],
                              name="input_z")
        is_training_pl = tf.placeholder(tf.bool, [], name='is_training_pl')
        learning_rate = tf.placeholder(tf.float32, shape=(), name="lr_pl")

        # models
        gen = config.decoder
        enc = config.encoder
        dis_xz = config.discriminator_xz
        dis_xx = config.discriminator_xx
        dis_zz = config.discriminator_zz

        # compile models
        with tf.variable_scope('encoder_model'):
            z_gen = enc(x_pl, is_training=is_training_pl,
                        do_spectral_norm=config.do_spectral_norm)

        with tf.variable_scope('generator_model'):
            x_gen = gen(z_pl, is_training=is_training_pl)
            rec_x = gen(z_gen, is_training=is_training_pl, reuse=True)

        with tf.variable_scope('encoder_model'):
            rec_z = enc(x_gen, is_training=is_training_pl, reuse=True,
                        do_spectral_norm=config.do_spectral_norm)

        with tf.variable_scope('discriminator_model_xz'):
            l_encoder, inter_layer_inp_xz = dis_xz(x_pl, z_gen,
                                                   is_training=is_training_pl,
                                                   do_spectral_norm=config.do_spectral_norm)
            l_generator, inter_layer_rct_xz = dis_xz(x_gen, z_pl,
                                                     is_training=is_training_pl,
                                                     reuse=True,
                                                     do_spectral_norm=config.do_spectral_norm)

        with tf.variable_scope('discriminator_model_xx'):
            x_logit_real, inter_layer_inp_xx = dis_xx(x_pl, x_pl,
                                                      is_training=is_training_pl,
                                                      do_spectral_norm=config.do_spectral_norm)
            x_logit_fake, inter_layer_rct_xx = dis_xx(x_pl, rec_x, is_training=is_training_pl,
                                                      reuse=True, do_spectral_norm=config.do_spectral_norm)

        with tf.variable_scope('discriminator_model_zz'):
            z_logit_real, _ = dis_zz(z_pl, z_pl, is_training=is_training_pl,
                                     do_spectral_norm=config.do_spectral_norm)
            z_logit_fake, _ = dis_zz(z_pl, rec_z, is_training=is_training_pl,
                                     reuse=True, do_spectral_norm=config.do_spectral_norm)

        with tf.name_scope('loss_functions'):
            # discriminator xz
            loss_dis_enc = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.ones_like(l_encoder), logits=l_encoder))
            loss_dis_gen = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.zeros_like(l_generator), logits=l_generator))
            dis_loss_xz = loss_dis_gen + loss_dis_enc

            # discriminator xx
            x_real_dis = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=x_logit_real, labels=tf.ones_like(x_logit_real))
            x_fake_dis = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=x_logit_fake, labels=tf.zeros_like(x_logit_fake))
            dis_loss_xx = tf.reduce_mean(x_real_dis + x_fake_dis)

            # discriminator zz
            z_real_dis = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=z_logit_real, labels=tf.ones_like(z_logit_real))
            z_fake_dis = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=z_logit_fake, labels=tf.zeros_like(z_logit_fake))
            dis_loss_zz = tf.reduce_mean(z_real_dis + z_fake_dis)

            loss_discriminator = dis_loss_xz + dis_loss_xx + dis_loss_zz if \
                config.allow_zz else dis_loss_xz + dis_loss_xx

            # generator and encoder
            gen_loss_xz = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.ones_like(l_generator), logits=l_generator))
            enc_loss_xz = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(
                labels=tf.zeros_like(l_encoder), logits=l_encoder))
            x_real_gen = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=x_logit_real, labels=tf.zeros_like(x_logit_real))
            x_fake_gen = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=x_logit_fake, labels=tf.ones_like(x_logit_fake))
            z_real_gen = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=z_logit_real, labels=tf.zeros_like(z_logit_real))
            z_fake_gen = tf.nn.sigmoid_cross_entropy_with_logits(
                logits=z_logit_fake, labels=tf.ones_like(z_logit_fake))

            cost_x = tf.reduce_mean(x_real_gen + x_fake_gen)
            cost_z = tf.reduce_mean(z_real_gen + z_fake_gen)

            cycle_consistency_loss = cost_x + cost_z if config.allow_zz else cost_x
            loss_generator = gen_loss_xz + cycle_consistency_loss
            loss_encoder = enc_loss_xz + cycle_consistency_loss

        with tf.name_scope('optimizers'):
            # control op dependencies for batch norm and trainable variables
            tvars = tf.trainable_variables()
            dxzvars = [var for var in tvars if 'discriminator_model_xz' in var.name]
            dxxvars = [var for var in tvars if 'discriminator_model_xx' in var.name]
            dzzvars = [var for var in tvars if 'discriminator_model_zz' in var.name]
            gvars = [var for var in tvars if 'generator_model' in var.name]
            evars = [var for var in tvars if 'encoder_model' in var.name]

            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            update_ops_gen = [x for x in update_ops if ('generator_model' in x.name)]
            update_ops_enc = [x for x in update_ops if ('encoder_model' in x.name)]
            update_ops_dis_xz = [x for x in update_ops if
                                 ('discriminator_model_xz' in x.name)]
            update_ops_dis_xx = [x for x in update_ops if
                                 ('discriminator_model_xx' in x.name)]
            update_ops_dis_zz = [x for x in update_ops if
                                 ('discriminator_model_zz' in x.name)]

            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate,
                                               beta1=0.5)

            with tf.control_dependencies(update_ops_gen):
                gen_op = optimizer.minimize(loss_generator, var_list=gvars,
                                            global_step=global_step)
            with tf.control_dependencies(update_ops_enc):
                enc_op = optimizer.minimize(loss_encoder, var_list=evars)

            with tf.control_dependencies(update_ops_dis_xz):
                dis_op_xz = optimizer.minimize(dis_loss_xz, var_list=dxzvars)

            with tf.control_dependencies(update_ops_dis_xx):
                dis_op_xx = optimizer.minimize(dis_loss_xx, var_list=dxxvars)

            with tf.control_dependencies(update_ops_dis_zz):
                dis_op_zz = optimizer.minimize(dis_loss_zz, var_list=dzzvars)

            # Exponential Moving Average for inference
            def train_op_with_ema_dependency(vars, op):
                ema = tf.train.ExponentialMovingAverage(decay=config.ema_decay)
                maintain_averages_op = ema.apply(vars)
                with tf.control_dependencies([op]):
                    train_op = tf.group(maintain_averages_op)
                return train_op, ema

            train_gen_op, gen_ema = train_op_with_ema_dependency(gvars, gen_op)
            train_enc_op, enc_ema = train_op_with_ema_dependency(evars, enc_op)
            train_dis_op_xz, xz_ema = train_op_with_ema_dependency(dxzvars,
                                                                   dis_op_xz)
            train_dis_op_xx, xx_ema = train_op_with_ema_dependency(dxxvars,
                                                                   dis_op_xx)
            train_dis_op_zz, zz_ema = train_op_with_ema_dependency(dzzvars,
                                                                   dis_op_zz)

        with tf.variable_scope('encoder_model'):
            z_gen_ema = enc(x_pl, is_training=is_training_pl,
                            getter=get_getter(enc_ema), reuse=True,
                            do_spectral_norm=config.do_spectral_norm)

        with tf.variable_scope('generator_model'):
            rec_x_ema = gen(z_gen_ema, is_training=is_training_pl,
                            getter=get_getter(gen_ema), reuse=True)
            x_gen_ema = gen(z_pl, is_training=is_training_pl,
                            getter=get_getter(gen_ema), reuse=True)

        with tf.variable_scope('discriminator_model_xx'):
            l_encoder_emaxx, inter_layer_inp_emaxx = dis_xx(x_pl, x_pl,
                                                            is_training=is_training_pl,
                                                            getter=get_getter(xx_ema),
                                                            reuse=True,
                                                            do_spectral_norm=config.do_spectral_norm)

            l_generator_emaxx, inter_layer_rct_emaxx = dis_xx(x_pl, rec_x_ema,
                                                              is_training=is_training_pl,
                                                              getter=get_getter(
                                                                  xx_ema),
                                                              reuse=True,
                                                              do_spectral_norm=config.do_spectral_norm)

        with tf.name_scope('Testing'):

            with tf.variable_scope('Scores'):
                score_ch = tf.nn.sigmoid_cross_entropy_with_logits(
                    labels=tf.ones_like(l_generator_emaxx),
                    logits=l_generator_emaxx)
                score_ch = tf.squeeze(score_ch)

                rec = x_pl - rec_x_ema
                rec = tf.contrib.layers.flatten(rec)
                score_l1 = tf.norm(rec, ord=1, axis=1,
                                   keep_dims=False, name='d_loss')
                score_l1 = tf.squeeze(score_l1)

                rec = x_pl - rec_x_ema
                rec = tf.contrib.layers.flatten(rec)
                score_l2 = tf.norm(rec, ord=2, axis=1,
                                   keep_dims=False, name='d_loss')
                score_l2 = tf.squeeze(score_l2)

                inter_layer_inp, inter_layer_rct = inter_layer_inp_emaxx, \
                                                   inter_layer_rct_emaxx
                fm = inter_layer_inp - inter_layer_rct
                fm = tf.contrib.layers.flatten(fm)
                score_fm = tf.norm(fm, ord=config.fm_degree, axis=1,
                                   keep_dims=False, name='d_loss')
                score_fm = tf.squeeze(score_fm)

                # weighted lp score
                diff = x_pl - rec_x_ema
                diff = tf.contrib.layers.flatten(diff)
                diff = tf.divide(diff, 0.3 * tf.abs(x_pl) + 0.2)

                score_wlp = tf.norm(diff, ord=1, axis=1, keep_dims=False, name='d_loss')
                score_wlp = tf.squeeze(score_wlp)

        if config.enable_sm:

            with tf.name_scope('summary'):
                with tf.name_scope('dis_summary'):
                    tf.summary.scalar('loss_discriminator', loss_discriminator, ['dis'])
                    tf.summary.scalar('loss_dis_encoder', loss_dis_enc, ['dis'])
                    tf.summary.scalar('loss_dis_gen', loss_dis_gen, ['dis'])
                    tf.summary.scalar('loss_dis_xz', dis_loss_xz, ['dis'])
                    tf.summary.scalar('loss_dis_xx', dis_loss_xx, ['dis'])
                    if config.allow_zz:
                        tf.summary.scalar('loss_dis_zz', dis_loss_zz, ['dis'])

                with tf.name_scope('gen_summary'):
                    tf.summary.scalar('loss_generator', loss_generator, ['gen'])
                    tf.summary.scalar('loss_encoder', loss_encoder, ['gen'])
                    tf.summary.scalar('loss_encgen_dxx', cost_x, ['gen'])
                    if config.allow_zz:
                        tf.summary.scalar('loss_encgen_dzz', cost_z, ['gen'])

                sum_op_dis = tf.summary.merge_all('dis')
                sum_op_gen = tf.summary.merge_all('gen')
                sum_op = tf.summary.merge([sum_op_dis, sum_op_gen])
                sum_op_im = tf.summary.merge_all('image')
                sum_op_valid = tf.summary.merge_all('v')

        self.__dict__.update(locals())

    def recon(self, x):
        return self.sess.run(self.rec_x, feed_dict={self.x_pl: x})

    def compute_fm_scores(self, x):
        feed_dict = {self.x_pl: x,
                     self.z_pl: np.random.normal(size=[x.shape[0], self.config.latent_dim]),
                     self.is_training_pl: False}

        return self.sess.run(self.score_fm, feed_dict=feed_dict)

    def get_anomaly_scores(self, x, type='fm'):
        feed_dict = {self.x_pl: x,
                     self.z_pl: np.random.normal(size=[x.shape[0], self.config.latent_dim]),
                     self.is_training_pl: False}

        if type == 'fm':
            return self.sess.run(self.score_fm, feed_dict=feed_dict)
        elif type == 'l1':
            return self.sess.run(self.score_l1, feed_dict=feed_dict)
        elif type == 'l2':
            return self.sess.run(self.score_l2, feed_dict=feed_dict)
        elif type == 'ch':
            return self.sess.run(self.score_ch, feed_dict=feed_dict)
        elif type == 'weighted_lp':
            return self.sess.run(self.score_wlp, feed_dict=feed_dict)
        else:
            raise Exception()

    def get_anomaly_scores_batch(self, x, batch_size=1024, type='fm'):
        if type == 'fm':
            score_node = self.score_fm
        elif type == 'l1':
            score_node = self.score_l1
        elif type == 'l2':
            score_node = self.score_l2
        elif type == 'ch':
            score_node = self.score_ch
        elif type == 'weighted_lp':
            score_node = self.score_wlp
        else:
            raise Exception()

        n = x.shape[0]
        scores = np.empty(n)
        n_batches = int(n / batch_size) + 1
        for t in range(n_batches):
            ran_from = t * batch_size
            ran_to = (t + 1) * batch_size
            ran_to = np.clip(ran_to, 0, n)

            x_batch = x[ran_from:ran_to]

            feed_dict = {self.x_pl: x_batch,
                         self.z_pl: np.random.normal(size=[x_batch.shape[0], self.config.latent_dim]),
                         self.is_training_pl: False}

            scores[ran_from, ran_to] = self.sess.run(score_node, feed_dict=feed_dict)

        return scores

    def weighted_lp(self, x, ord=1, eps=1, a=0):
        feed_dict = {self.x_pl: x,
                     self.z_pl: np.random.normal(size=[x.shape[0], self.config.latent_dim]),
                     self.is_training_pl: False}

        diff = self.sess.run(self.x_pl - self.rec_x_ema, feed_dict=feed_dict)
        diff = np.abs(diff)
        diff = diff / (a * np.abs(x) + eps)

        return np.sum(diff, axis=1)

    def compute_all_scores(self, x):
        feed_dict = {self.x_pl: x,
                     self.z_pl: np.random.normal(size=[x.shape[0], self.config.latent_dim]),
                     self.is_training_pl: False}
        scores = [self.score_fm, self.score_l1, self.score_l2, self.score_ch]

        return self.sess.run(scores, feed_dict=feed_dict)

    def fit(self, x, max_epoch, logdir, evaluator, weights_file=None):
        sess = self.sess
        saver = tf.train.Saver(max_to_keep=1000)
        writer = tf.summary.FileWriter(logdir, sess.graph)

        if weights_file is None:
            # run initialization
            sess.run(tf.global_variables_initializer())
            sess.run(tf.assign(self.global_step, 0))
        else:
            self.load(weights_file)

        batch_size = self.config.batch_size
        nr_batches_train = int(x.shape[0] / batch_size)

        print('Start training...')
        # EPOCHS
        for epoch in range(max_epoch):
            print('---------- EPOCH %s ----------' % epoch)

            begin = time.time()

            # construct randomly shuffled batches
            trainx = sklearn.utils.shuffle(x)
            trainx_copy = sklearn.utils.shuffle(x)

            # fit one batch
            for t in range(nr_batches_train):
                ran_from = t * batch_size
                ran_to = (t + 1) * batch_size

                # train discriminator
                feed_dict = {self.x_pl: trainx[ran_from:ran_to],
                             self.z_pl: np.random.normal(size=[batch_size, self.config.latent_dim]),
                             self.is_training_pl: True,
                             self.learning_rate: self.config.learning_rate}

                _, _, _, step = sess.run([self.train_dis_op_xz,
                                          self.train_dis_op_xx,
                                          self.train_dis_op_zz,
                                          self.global_step],
                                         feed_dict=feed_dict)

                # train generator and encoder
                feed_dict = {self.x_pl: trainx_copy[ran_from:ran_to],
                             self.z_pl: np.random.normal(size=[batch_size, self.config.latent_dim]),
                             self.is_training_pl: True,
                             self.learning_rate: self.config.learning_rate}
                sess.run([self.train_gen_op,
                          self.train_enc_op],
                         feed_dict=feed_dict)

                # end of batch
                if self.config.enable_sm and step % self.config.sm_write_freq == 0:
                    display_progression_epoch(begin, t, nr_batches_train)
                    sm = sess.run(self.sum_op, feed_dict=feed_dict)
                    writer.add_summary(sm, step)

                if self.config.enable_eval and step % self.config.eval_freq == 0:
                    print('evaluating at step %s' % step)
                    evaluator.evaluate(self, step, {})
                    evaluator.save_results(logdir)

                    # add some metrics to summary
                    # sm = tf.Summary()
                    # sm.value.add(tag='AUROC', simple_value=evaluator.hist['auroc'][-1])
                    # writer.add_summary(sm, step)

                if self.config.enable_checkpoint_save and step % self.config.checkpoint_freq == 0:
                    print('saving checkpoint at step %s' % step)
                    saver.save(sess, logdir + '/model', global_step=step)

            # end of epoch
            print("Epoch %d | time = %ds" % (epoch, time.time() - begin))

    def load(self, file):
        saver = tf.train.Saver()
        saver.restore(self.sess, file)

    def save(self, path):
        pass


def get_getter(ema):  # to update neural net with moving avg variables, suitable for ss learning cf Saliman
    def ema_getter(getter, name, *args, **kwargs):
        var = getter(name, *args, **kwargs)
        ema_var = ema.average(var)
        return ema_var if ema_var else var

    return ema_getter


def display_progression_epoch(start_time, j, id_max):
    batch_progression = int((j / id_max) * 100)
    sys.stdout.write('time: %s sec | progression: %s / %s (%s %%)' %
                     (int(time.time() - start_time), j, id_max, batch_progression) + chr(13))
    _ = sys.stdout.flush
