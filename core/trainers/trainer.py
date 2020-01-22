import copy
from tqdm import tqdm
import torch
import math

from util.general_functions import get_model, get_optimizer, make_data_loader, get_loss_function, get_flat_images
from util.lr_scheduler import LR_Scheduler
from util.summary import TensorboardSummary
from constants import *
from util.ssim import MS_SSIM, SSIM


class Trainer(object):

    def __init__(self, args):
        self.args = args
        self.best_loss = math.inf

        self.model = get_model(args)
        if args.save_best_model:
            self.best_model = copy.deepcopy(self.model)
        self.optimizer = get_optimizer(self.model, args)
        self.summary = TensorboardSummary(args)
        self.ssim, self.ms_ssim = SSIM(), MS_SSIM()

        if args.trainval:
            self.train_loader, self.val_loader = make_data_loader(args, TRAINVAL), make_data_loader(args, TEST)
        else:
            self.train_loader, self.test_loader = make_data_loader(args, TRAIN), make_data_loader(args, TEST)

        self.criterion = get_loss_function(args.loss_type)
        self.scheduler = LR_Scheduler(args.lr_policy, args.lr, args.epochs, len(self.train_loader))

        if args.second_loss:
            self.second_criterion = get_loss_function(MS_SSIM_LOSS)

    def run_epoch(self, epoch, split=TRAIN):
        loss = 0.0
        ssim_values, ms_ssim_values = [], []

        if split == TRAIN:
            self.model.train()
            loader = self.train_loader
        elif split == VAL:
            self.model.eval()
            loader = self.val_loader
        else:
            self.model.eval()
            loader = self.test_loader

        bar = tqdm(loader)
        num_img = len(loader)

        for i, sample in enumerate(bar):
            with torch.autograd.set_detect_anomaly(True):
                image = sample[0]
                target = sample[1]

                if self.args.cuda:
                    image, target = image.cuda(), target.cuda()

                if split == TRAIN:
                    self.scheduler(self.optimizer, i, epoch, self.best_loss)
                    self.optimizer.zero_grad()

                    if self.args.refine_network:
                        first_output, output = self.model(image)
                    else:
                        output = self.model(image)
                else:
                    with torch.no_grad():
                        if self.args.refine_network:
                            first_output, output = self.model(image)
                        else:
                            output = self.model(image)

                loss = self.criterion(output, target)
                if self.args.refine_network:
                    refine_loss = self.criterion(first_output, target)
                    loss += refine_loss

                if self.args.second_loss:
                    flat_output_img, flat_target_img = get_flat_images(self.args.dataset, image, output, target)
                    second_loss = self.second_criterion(flat_output_img, flat_target_img)
                    loss += second_loss

                    if self.args.refine_network:
                        flat_first_output_img, flat_target_img = get_flat_images(self.args.dataset, image, first_output, target)
                        third_loss = self.second_criterion(flat_first_output_img, flat_target_img)
                        loss += third_loss

                if split == TRAIN:
                    loss.backward()

                    if self.args.clip > 0:
                        if self.args.gpu_ids:
                            torch.nn.utils.clip_grad_norm_(self.model.module().parameters(), self.args.clip)
                        else:
                            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip)

                    self.optimizer.step()

                if split == TEST:
                    ssim_values.append(self.ssim.forward(output, target))
                    ms_ssim_values.append(self.ms_ssim.forward(output, target))

                # Show 10 * 3 inference results each epoch
                if split != VISUALIZATION and i % (num_img // 10) == 0:
                    self.summary.visualize_image(image, target, output, split=split)
                elif split == VISUALIZATION:
                    self.summary.visualize_image(image, target, output, split=split)

                loss += loss.item()
                bar.set_description(split +' loss: %.3f' % (loss / (i + 1)))

        if split == TEST:
            ssim = sum(ssim_values) / len(ssim_values)
            ms_ssim = sum(ms_ssim_values) / len(ms_ssim_values)
            self.summary.add_scalar(split + '/ssim', ssim, epoch)
            self.summary.add_scalar(split + '/ms_ssim', ms_ssim, epoch)

            if loss < self.best_loss:
                self.best_loss = loss
                if self.args.save_best_model:
                    self.best_model = copy.deepcopy(self.model)

        self.summary.add_scalar(split + '/total_loss_epoch', loss, epoch)
        print('[Epoch: %d, numImages: %5d]' % (epoch, i * self.args.batch_size + image.data.shape[0]))


    def save_network(self):
        self.summary.save_network(self.model)

    def load_network(self):
        self.best_model = get_model(self.args)
        self.best_model.load_state_dict(torch.load(''))