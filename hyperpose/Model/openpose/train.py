#!/usr/bin/env python3

import math
import multiprocessing
import os
import cv2
import time
import sys
import json
from tqdm import tqdm
import numpy as np
import matplotlib

matplotlib.use('Agg')
import tensorflow as tf
import tensorlayer as tl
import _pickle as cPickle
from functools import partial, reduce
from .processor import PreProcessor, Visualizer
from .utils import tf_repeat, draw_results
from .utils import get_parts, get_limbs, get_flip_list
from ..augmentor import Augmentor
from ..common import log, KUNGFU, MODEL, get_optim, init_log, regulize_loss
from ..domainadapt import Discriminator
from ..common import decode_mask,get_num_parallel_calls
from ..metrics import TimeMetric, MetricManager


def _data_aug_fn(image, ground_truth, augmentor, preprocessor, data_format="channels_first"):
    """Data augmentation function."""
    # restore data
    image = image.numpy()
    ground_truth = cPickle.loads(ground_truth.numpy())
    annos = ground_truth["kpt"]
    labeled = ground_truth["labeled"]
    meta_mask = ground_truth["mask"]

    # decode mask
    mask = decode_mask(meta_mask)
    if(type(mask) != np.ndarray):
        mask = np.ones_like(image)[:,:,0].astype(np.uint8)

    # general augmentaton process
    image, annos, mask = augmentor.process(image=image, annos=annos, mask_valid=mask)
    mask = mask[:,:,np.newaxis]
    image = image * mask

    # generate result including heatmap and vectormap
    target_x = preprocessor.process(annos=annos, mask_valid=mask)
    target_x = cPickle.dumps(target_x)

    # generate labeled value for domain adaptation
    labeled = np.float32(labeled)

    # TODO: channel format
    if (data_format == "channels_first"):
        image = np.transpose(image, [2, 0, 1])
        mask = np.transpose(mask, [2, 0, 1])

    return image, mask, target_x, labeled


def _map_fn(img_list, annos, data_aug_fn):
    """TF Dataset pipeline."""

    # load data
    image = tf.io.read_file(img_list)
    image = tf.image.decode_jpeg(image, channels=3)  # get RGB with 0~1
    image = tf.image.convert_image_dtype(image, dtype=tf.float32)

    # data augmentation using affine transform and get paf maps
    image, mask, target_x, labeled = tf.py_function(data_aug_fn, [image, annos],
                                                    [tf.float32, tf.float32, tf.string, tf.float32])

    # data augmentaion using tf
    image = tf.image.random_brightness(image, max_delta=35. / 255.)  # 64./255. 32./255.)  caffe -30~50
    image = tf.image.random_contrast(image, lower=0.5, upper=1.5)  # lower=0.2, upper=1.8)  caffe 0.3~1.5
    image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=1.0)

    return image, mask, target_x, labeled


def get_paramed_map_fn(augmentor, preprocessor, data_format="channels_first"):
    paramed_data_aug_fn = partial(_data_aug_fn, augmentor=augmentor, preprocessor=preprocessor, data_format=data_format)
    paramed_map_fn = partial(_map_fn, data_aug_fn=paramed_data_aug_fn)
    return paramed_map_fn


def single_train(train_model, dataset, config):
    '''Single train pipeline of Openpose class models

    input model and dataset, the train pipeline will start automaticly
    the train pipeline will:
    1.store and restore ckpt in directory ./save_dir/model_name/model_dir
    2.log loss information in directory ./save_dir/model_name/log.txt
    3.visualize model output periodly during training in directory ./save_dir/model_name/train_vis_dir
    the newest model is at path ./save_dir/model_name/model_dir/newest_model.npz

    Parameters
    ----------
    arg1 : tensorlayer.models.MODEL
        a preset or user defined model object, obtained by Model.get_model() function
    
    arg2 : dataset
        a constructed dataset object, obtained by Dataset.get_dataset() function
    
    
    Returns
    -------
    None
    '''

    init_log(config)
    # train hyper params
    # dataset params
    total_step = config.train.n_step
    batch_size = config.train.batch_size
    # learning rate params
    lr_init = config.train.lr_init
    lr_decay_factor = config.train.lr_decay_factor
    lr_decay_steps = [200000, 300000, 360000, 420000, 480000, 540000, 600000, 700000, 800000, 900000]
    weight_decay_factor = config.train.weight_decay_factor
    # log and checkpoint params
    log_interval = config.log.log_interval
    vis_interval =  config.train.vis_interval
    save_interval = config.train.save_interval
    vis_dir = config.train.vis_dir

    # model hyper params
    hin = train_model.hin
    win = train_model.win
    hout = train_model.hout
    wout = train_model.wout
    parts, limbs, colors = train_model.parts, train_model.limbs, train_model.colors
    data_format = train_model.data_format
    model_dir = config.model.model_dir
    pretrain_model_dir = config.pretrain.pretrain_model_dir
    pretrain_model_path = f"{pretrain_model_dir}/newest_{train_model.backbone.name}.npz"

    # processors
    augmentor = Augmentor(hin=hin, win=win, angle_min=-30, angle_max=30, zoom_min=0.5, zoom_max=0.8, flip_list=None)
    preprocessor = PreProcessor(parts=parts, limbs=limbs, hin=hin, win=win, hout=hout, wout=wout, colors=colors,\
                                                                                    data_format=data_format)
    visualizer = Visualizer(save_dir=vis_dir)
    
    # metrics
    metric_manager = MetricManager()

    # initializing train dataset
    train_dataset = dataset.get_train_dataset()
    epoch_size = dataset.get_train_datasize()//batch_size
    paramed_map_fn = get_paramed_map_fn(augmentor=augmentor, preprocessor=preprocessor, data_format=data_format)
    train_dataset = train_dataset.shuffle(buffer_size=4096).repeat()
    train_dataset = train_dataset.map(paramed_map_fn, num_parallel_calls=get_num_parallel_calls())
    train_dataset = train_dataset.batch(config.train.batch_size)
    train_dataset = train_dataset.prefetch(3)
    train_dataset_iter = iter(train_dataset)

    #train configure
    save_step = tf.Variable(1, trainable=False)
    save_lr = tf.Variable(lr_init, trainable=False)
    opt = tf.keras.optimizers.Adam(learning_rate=save_lr)
    domainadapt_flag = config.data.domainadapt_flag
    total_epoch = total_step//epoch_size

    #domain adaptation params
    if (not domainadapt_flag):
        ckpt = tf.train.Checkpoint(save_step=save_step, save_lr=save_lr, opt=opt)
    else:
        log("domain adaptaion enabled!")
        feature_hin = train_model.hin/train_model.backbone.scale_size
        feature_win = train_model.win/train_model.backbone.scale_size
        in_channels = train_model.backbone.out_channels
        adapt_dis = Discriminator(feature_hin, feature_win, in_channels, data_format=data_format)
        opt_d = tf.keras.optimizers.Adam(learning_rate=save_lr)
        ckpt = tf.train.Checkpoint(save_step=save_step, save_lr=save_lr, opt=opt, opt_d=opt_d)

    #load from ckpt
    ckpt_manager = tf.train.CheckpointManager(ckpt, model_dir, max_to_keep=3)
    try:
        log("loading ckpt...")
        ckpt.restore(ckpt_manager.latest_checkpoint)
    except:
        log("ckpt_path doesn't exist, step and optimizer are initialized")
    #load pretrained backbone
    try:
        log("loading pretrained backbone...")
        tl.files.load_and_assign_npz_dict(name=pretrain_model_path, network=train_model.backbone, skip=True)
    except:
        log("pretrained backbone doesn't exist, model backbone are initialized")
    #load model weights
    try:
        log("loading saved training model weights...")
        train_model.load_weights(os.path.join(model_dir, "newest_model.npz"))
    except:
        log("model_path doesn't exist, model parameters are initialized")
    if (domainadapt_flag):
        try:
            log("loading saved domain adaptation discriminator weight...")
            adapt_dis.load_weights(os.path.join(model_dir, "newest_discriminator.npz"))
        except:
            log("discriminator path doesn't exist, discriminator parameters are initialized")

    
    print(f"single training using learning rate:{lr_init} batch_size:{batch_size}")
    step = save_step.numpy()
    lr = save_lr.numpy()

    for lr_decay_step in lr_decay_steps:
        if (step > lr_decay_step):
            lr = lr * lr_decay_factor

    # optimize one step
    def optimize_step(image, mask, target_x, labeled, train_model, metric_manager: MetricManager):
        with tf.GradientTape() as tape:
            # optimize model
            predict_x = train_model.forward(x=image, is_train=True)
            total_loss = train_model.cal_loss(predict_x=predict_x, target_x=target_x, \
                                                        mask=mask, metric_manager=metric_manager)
            if (domainadapt_flag):
                g_adapt_loss = adapt_dis.cal_loss(x=predict_x["backbone_feature"], label=1 - labeled)
                metric_manager.update("model/adapt_loss", g_adapt_loss)
                total_loss += g_adapt_loss
            # tape
            gradients = tape.gradient(total_loss, train_model.trainable_weights)
            opt.apply_gradients(zip(gradients, train_model.trainable_weights))

            # optimize dis
            if (domainadapt_flag):
                # optimize discriminator
                d_adapt_loss = adapt_dis.cal_loss(x=predict_x["backbone_feature"], label=labeled)
                metric_manager.update("dis/adapt_loss", d_adapt_loss)
                # tape
                d_gradients = tape.gradient(d_adapt_loss, adapt_dis.trainable_weights)
                opt.apply_gradients(zip(d_gradients, adapt_dis.trainable_weights))
        return predict_x

    # formal training procedure
    train_model.train()
    cur_epoch = step // epoch_size +1
    log(f"Start Training- total_epoch: {total_epoch} total_step: {total_step} current_epoch:{cur_epoch} "\
        +f"current_step:{step} batch_size:{batch_size} lr_init:{lr_init} lr_decay_steps:{lr_decay_steps} "\
        +f"lr_decay_factor:{lr_decay_factor} weight_decay_factor:{weight_decay_factor}" )
    for epoch_idx in range(cur_epoch,total_epoch):
        log(f"Epoch {epoch_idx}/{total_epoch}:")
        for epoch_step in tqdm(range(0,epoch_size)):
            step+=1
            metric_manager.start_timing()
            image, mask, target_list, labeled = next(train_dataset_iter)
            # extract gt_label
            target_list = [cPickle.loads(target) for target in target_list.numpy()]
            target_x = {key:[] for key,value in target_list[0].items()}
            target_x = reduce(lambda x, y: {key:x[key]+[y[key]] for key,value in x.items()},[target_x]+target_list)
            target_x = {key:np.stack(value) for key,value in target_x.items()}

            # learning rate decay
            if (step in lr_decay_steps):
                new_lr_decay = lr_decay_factor**(lr_decay_steps.index(step) + 1)
                lr = lr_init * new_lr_decay

            # optimize one step
            predict_x=optimize_step(image, mask, target_x, labeled, train_model, metric_manager)

            # log info periodly
            if ((step != 0) and (step % log_interval) == 0):
                log(f"Train Epoch={epoch_idx}, Step={step} / {total_step}: learning_rate: {lr} {metric_manager.report_timing()}\n"\
                        +f"{metric_manager.report_train()} ")

            # visualize periodly
            if ((step != 0) and (step % vis_interval) == 0):
                log(f"Visualizing prediction maps and target maps")
                visualizer.draw_results(image.numpy(), mask.numpy(), predict_x, target_x, name=f"train_{step}")

            # save result and ckpt periodly
            if ((step!= 0) and (step % save_interval) == 0):
                # save ckpt
                log("saving model ckpt and result...")
                save_step.assign(step)
                save_lr.assign(lr)
                ckpt_save_path = ckpt_manager.save()
                log(f"ckpt save_path:{ckpt_save_path} saved!\n")
                # save train model
                model_save_path = os.path.join(model_dir, "newest_model.npz")
                train_model.save_weights(model_save_path)
                log(f"model save_path:{model_save_path} saved!\n")
                # save discriminator model
                if (domainadapt_flag):
                    dis_save_path = os.path.join(model_dir, "newest_discriminator.npz")
                    adapt_dis.save_weights(dis_save_path)
                    log(f"discriminator save_path:{dis_save_path} saved!\n")

def parallel_train(train_model, dataset, config):
    '''Parallel train pipeline of openpose class models

    input model and dataset, the train pipeline will start automaticly
    the train pipeline will:
    1.store and restore ckpt in directory ./save_dir/model_name/model_dir
    2.log loss information in directory ./save_dir/model_name/log.txt
    3.visualize model output periodly during training in directory ./save_dir/model_name/train_vis_dir
    the newest model is at path ./save_dir/model_name/model_dir/newest_model.npz

    Parameters
    ----------
    arg1 : tensorlayer.models.MODEL
        a preset or user defined model object, obtained by Model.get_model() function
    
    arg2 : dataset
        a constructed dataset object, obtained by Dataset.get_dataset() function
    
    
    Returns
    -------
    None
    '''

    init_log(config)
    #train hyper params
    #dataset params
    total_step = config.train.total_step
    batch_size = config.train.batch_size
    #learning rate params
    lr_init = config.train.lr_init
    lr_decay_factor = config.train.lr_decay_factor
    lr_decay_steps = [200000, 300000, 360000, 420000, 480000, 540000, 600000, 700000, 800000, 900000]
    weight_decay_factor = config.train.weight_decay_factor
    #log and checkpoint params
    log_interval = config.log.log_interval
    save_interval = config.train.save_interval
    vis_dir = config.train.vis_dir

    #model hyper params
    n_pos = train_model.n_pos
    hin = train_model.hin
    win = train_model.win
    hout = train_model.hout
    wout = train_model.wout
    parts, limbs, colors = train_model.parts, train_model.limbs, train_model.colors
    data_format = train_model.data_format
    model_dir = config.model.model_dir
    pretrain_model_dir = config.pretrain.pretrain_model_dir
    pretrain_model_path = f"{pretrain_model_dir}/newest_{train_model.backbone.name}.npz"

    #import kungfu
    from kungfu import current_cluster_size, current_rank
    from kungfu.tensorflow.initializer import broadcast_variables
    from kungfu.tensorflow.optimizers import SynchronousSGDOptimizer, SynchronousAveragingOptimizer, PairAveragingOptimizer

    print(f"parallel training using learning rate:{lr_init} batch_size:{batch_size}")
    #training dataset configure with shuffle,augmentation,and prefetch
    train_dataset = dataset.get_train_dataset()
    augmentor = Augmentor(hin=hin, win=win, angle_min=-30, angle_max=30, zoom_min=0.5, zoom_max=0.8, flip_list=None)
    preprocessor = PreProcessor(parts=parts,
                                limbs=limbs,
                                hin=hin,
                                win=win,
                                hout=hout,
                                wout=wout,
                                colors=colors,
                                data_format=data_format)
    paramed_map_fn = get_paramed_map_fn(augmentor=augmentor, preprocessor=preprocessor, data_format=data_format)
    train_dataset = train_dataset.shuffle(buffer_size=4096)
    train_dataset = train_dataset.shard(num_shards=current_cluster_size(), index=current_rank())
    train_dataset = train_dataset.repeat()
    train_dataset = train_dataset.map(paramed_map_fn, num_parallel_calls=4)
    train_dataset = train_dataset.batch(batch_size)
    train_dataset = train_dataset.prefetch(64)

    #train model configure
    step = tf.Variable(1, trainable=False)
    lr = tf.Variable(lr_init, trainable=False)
    if (config.model.model_type == MODEL.Openpose):
        opt = tf.keras.optimizers.RMSprop(learning_rate=lr)
    else:
        opt = tf.keras.optimizers.Adam(learning_rate=lr)
    ckpt = tf.train.Checkpoint(step=step, optimizer=opt, lr=lr)
    ckpt_manager = tf.train.CheckpointManager(ckpt, model_dir, max_to_keep=3)

    #load from ckpt
    try:
        log("loading ckpt...")
        ckpt.restore(ckpt_manager.latest_checkpoint)
    except:
        log("ckpt_path doesn't exist, step and optimizer are initialized")
    #load pretrained backbone
    try:
        log("loading pretrained backbone...")
        tl.files.load_and_assign_npz_dict(name=pretrain_model_path, network=train_model.backbone, skip=True)
    except:
        log("pretrained backbone doesn't exist, model backbone are initialized")
    #load model weights
    try:
        train_model.load_weights(os.path.join(model_dir, "newest_model.npz"))
    except:
        log("model_path doesn't exist, model parameters are initialized")

    # KungFu configure
    kungfu_option = config.train.kungfu_option
    if kungfu_option == KUNGFU.Sync_sgd:
        print("using Kungfu.SynchronousSGDOptimizer!")
        opt = SynchronousSGDOptimizer(opt)
    elif kungfu_option == KUNGFU.Sync_avg:
        print("using Kungfu.SynchronousAveragingOptimize!")
        opt = SynchronousAveragingOptimizer(opt)
    elif kungfu_option == KUNGFU.Pair_avg:
        print("using Kungfu.PairAveragingOptimizer!")
        opt = PairAveragingOptimizer(opt)

    total_step = total_step // current_cluster_size() + 1  # KungFu
    for step_idx, step in enumerate(lr_decay_steps):
        lr_decay_steps[step_idx] = step // current_cluster_size() + 1  # KungFu

    #optimize one step
    @tf.function
    def one_step(image, gt_label, mask, train_model, is_first_batch=False):
        step.assign_add(1)
        with tf.GradientTape() as tape:
            gt_conf = gt_label[:, :n_pos, :, :]
            gt_paf = gt_label[:, n_pos:, :, :]
            pd_conf, pd_paf, stage_confs, stage_pafs = train_model.forward(image, is_train=True)

            pd_loss, loss_confs, loss_pafs = train_model.cal_loss(gt_conf, gt_paf, mask, stage_confs, stage_pafs)
            re_loss = regulize_loss(train_model, weight_decay_factor)
            total_loss = pd_loss + re_loss

        gradients = tape.gradient(total_loss, train_model.trainable_weights)
        opt.apply_gradients(zip(gradients, train_model.trainable_weights))
        #Kung fu
        if (is_first_batch):
            broadcast_variables(train_model.all_weights)
            broadcast_variables(opt.variables())
        return gt_conf, gt_paf, pd_conf, pd_paf, total_loss, re_loss

    #train each step
    tic = time.time()
    train_model.train()
    log(f"Worker {current_rank()}: Initialized")
    log('Start - total_step: {} batch_size: {} lr_init: {} lr_decay_steps: {} lr_decay_factor: {}'.format(
        total_step, batch_size, lr_init, lr_decay_steps, lr_decay_factor))
    for image, gt_label, mask in train_dataset:
        #learning rate decay
        if (step in lr_decay_steps):
            new_lr_decay = lr_decay_factor**(float(lr_decay_steps.index(step) + 1))
            lr = lr_init * new_lr_decay
        #optimize one step
        gt_conf,gt_paf,pd_conf,pd_paf,total_loss,re_loss=one_step(image.numpy(),gt_label.numpy(),mask.numpy(),\
            train_model,step==0)
        #save log info periodly
        if ((step.numpy() != 0) and (step.numpy() % log_interval) == 0):
            tic = time.time()
            log('Total Loss at iteration {} / {} is: {} Learning rate {} l2_loss {} time:{}'.format(
                step.numpy(), total_step, total_loss, lr.numpy(), re_loss,
                time.time() - tic))

        #save result and ckpt periodly
        if ((step != 0) and (step % save_interval) == 0 and current_rank() == 0):
            log("saving model ckpt and result...")
            draw_results(image.numpy(), gt_conf.numpy(), pd_conf.numpy(), gt_paf.numpy(), pd_paf.numpy(), mask.numpy(),\
                 vis_dir,'train_%d_' % step)
            ckpt_save_path = ckpt_manager.save()
            log(f"ckpt save_path:{ckpt_save_path} saved!\n")
            model_save_path = os.path.join(model_dir, "newest_model.npz")
            train_model.save_weights(model_save_path)
            log(f"model save_path:{model_save_path} saved!\n")

        #training finished
        if (step == total_step):
            break
