import logging
import math
import os
import time
from typing import Any, Optional

import numpy as np
import torch
import tqdm
from timm.utils import ModelEmaV2
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from src.metrics import AccuracyMetric, LossMetric
from src.utils import weight_diff_norm, weight_norm


class Trainer:
    """Model trainer

    Args:
        model: model to train
        loss_fn: loss function
        optimizer: model optimizer
        epochs: number of epochs
        device: device to train the model on
        train_loader: training dataloader
        val_loader: validation dataloader
        scheduler: learning rate scheduler
        update_sched_on_iter: whether to call the scheduler every iter or every epoch
        grad_clip_max_norm: gradient clipping max norm (disabled if None)
        writer: writer which logs metrics to TensorBoard (disabled if None)
        save_path: folder in which to save models (disabled if None)
        checkpoint_path: path to model checkpoint, to resume training
        averaged_model: optional averaged model
        save_preds: whether to save predictions for further analysis

    """

    def __init__(
        self,
        model: torch.nn.Module,
        loss_fn: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        epochs: int,
        device: torch.device,
        train_loader: DataLoader,
        val_loader: Optional[DataLoader] = None,
        scheduler: Optional[Any] = None,  # Type: torch.optim.lr_scheduler._LRScheduler
        update_sched_on_iter: bool = False,
        grad_clip_max_norm: Optional[float] = None,
        writer: Optional[SummaryWriter] = None,
        save_path: Optional[str] = None,
        checkpoint_path: Optional[str] = None,
        averaged_model: Optional[ModelEmaV2] = None,
        save_preds: bool = False,
    ) -> None:

        # Logging
        self.logger = logging.getLogger()
        self.writer = writer

        # Saving
        self.save_path = save_path
        self.save_preds = save_preds

        # Device
        self.device = device

        # Data
        self.train_loader = train_loader
        self.val_loader = val_loader

        # Model
        self.model = model
        self.loss_fn = loss_fn
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.update_sched_on_iter = update_sched_on_iter
        self.grad_clip_max_norm = grad_clip_max_norm
        self.epochs = epochs
        self.start_epoch = 0

        # Averaged model
        self.averaged_model = averaged_model

        if checkpoint_path:
            self._load_from_checkpoint(checkpoint_path)

        # Metrics
        self.train_loss_metric = LossMetric()
        self.train_acc_metric = AccuracyMetric(k=1)

        self.val_loss_metric = LossMetric()
        self.val_acc_metric = AccuracyMetric(k=1, track_preds=self.save_preds)

        self.avg_model_loss_metric = LossMetric()
        self.avg_model_acc_metric = AccuracyMetric(k=1, track_preds=self.save_preds)

        self.weight_diff = None
        self.best_val_loss = math.inf
        self.best_avg_val_loss = math.inf

    def train(self) -> None:
        """Trains the model"""
        self.logger.info("Beginning training")
        start_time = time.time()

        for epoch in range(self.start_epoch, self.epochs):
            start_epoch_time = time.time()
            self._train_loop(epoch)

            if self.val_loader is not None:
                self._val_loop(epoch, on_averaged=False)

                if self.averaged_model is not None:
                    self._val_loop(epoch, on_averaged=True)

            epoch_time = time.time() - start_epoch_time
            self._end_loop(epoch, epoch_time)

        train_time_h = (time.time() - start_time) / 3600
        self.logger.info(f"Finished training! Total time: {train_time_h:.2f}h\n")
        self._save_model(os.path.join(self.save_path, "final_model.pt"), self.epochs)
        if self.averaged_model is not None:
            self._save_averaged_model(
                os.path.join(self.save_path, "final_averaged_model.pt"), self.epochs
            )

    def _train_loop(self, epoch: int) -> None:
        """
        Regular train loop

        Args:
            epoch: current epoch
        """
        # Progress bar
        pbar = tqdm.tqdm(total=len(self.train_loader), leave=False)
        pbar.set_description(f"Epoch {epoch} | Train")

        # Set to train
        self.model.train()

        # Loop
        for data, target in self.train_loader:
            # To device
            data, target = data.to(self.device), target.to(self.device)

            # Forward + backward
            self.optimizer.zero_grad()
            out = self.model(data)
            loss = self.loss_fn(out, target)
            loss.backward()

            if self.grad_clip_max_norm is not None:
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.grad_clip_max_norm
                )

            self.optimizer.step()

            # Update averaged model
            if self.averaged_model is not None:
                self.averaged_model.update(self.model)

            # Update scheduler if it is iter-based
            if self.scheduler is not None and self.update_sched_on_iter:
                self.scheduler.step()

            # Update metrics
            self.train_loss_metric.update(loss.item(), data.shape[0])
            self.train_acc_metric.update(out, target)

            # Update progress bar
            pbar.update()
            pbar.set_postfix_str(f"Loss: {loss.item():.3f}", refresh=False)

        # Update scheduler if it is epoch-based
        if self.scheduler is not None and not self.update_sched_on_iter:
            self.scheduler.step()

        pbar.close()

    def _val_loop(self, epoch: int, on_averaged=False) -> None:
        """
        Standard validation loop

        Args:
            epoch: current epoch
        """
        # Progress bar
        pbar = tqdm.tqdm(total=len(self.val_loader), leave=False)
        if not on_averaged:
            pbar.set_description(f"Epoch {epoch} | Validation")
        else:
            pbar.set_description(f"Epoch {epoch} | Averaged model validation")

        # Set to eval
        self.model.eval()

        # Loop
        for data, target in self.val_loader:
            with torch.no_grad():
                # To device
                data, target = data.to(self.device), target.to(self.device)

                # Forward
                if not on_averaged:
                    out = self.model(data)
                else:
                    out = self.averaged_model.module(data)

                loss = self.loss_fn(out, target)

                # Update metrics
                if not on_averaged:
                    self.val_loss_metric.update(loss.item(), data.shape[0])
                    self.val_acc_metric.update(out, target)
                else:
                    self.avg_model_loss_metric.update(loss.item(), data.shape[0])
                    self.avg_model_acc_metric.update(out, target)

                # Update progress bar
                pbar.update()
                pbar.set_postfix_str(f"Loss: {loss.item():.3f}", refresh=False)

        pbar.close()

    def _end_loop(self, epoch: int, epoch_time: float):
        if self.averaged_model is not None:
            self.weight_diff = weight_diff_norm(self.model, self.averaged_model)

        # Print epoch results
        self.logger.info(self._epoch_str(epoch, epoch_time))

        # Write to tensorboard
        if self.writer is not None:
            self._write_to_tb(epoch)

        # Save model
        if self.save_path is not None:
            self._save_model(os.path.join(self.save_path, "most_recent.pt"), epoch)
            # Save averaged model if loss is minimal
            if self.val_loader is not None:
                val_loss = self.val_loss_metric.compute()
                if self.best_val_loss > val_loss:
                    self.best_val_loss = val_loss
                    self._save_model(
                        os.path.join(self.save_path, "best_model.pt"), epoch
                    )

            if self.averaged_model is not None:
                self._save_averaged_model(
                    os.path.join(self.save_path, "averaged_most_recent.pt"), epoch
                )
                # Save averaged model if loss is minimal
                if self.val_loader is not None:
                    avg_val_loss = self.avg_model_loss_metric.compute()
                    if self.best_avg_val_loss > avg_val_loss:
                        self.best_avg_val_loss = avg_val_loss
                        self._save_averaged_model(
                            os.path.join(self.save_path, "best_averaged_model.pt"),
                            epoch,
                        )

            # Save preds
            if self.save_preds:
                preds_dir = os.path.join(self.save_path, "preds")
                os.makedirs(preds_dir, exist_ok=True)

                preds = self.val_acc_metric.get_preds()
                np.save(os.path.join(preds_dir, f"{epoch}"), preds)

                preds_average = self.avg_model_acc_metric.get_preds()
                np.save(os.path.join(preds_dir, f"{epoch}_average"), preds_average)

        # Clear metrics
        self.train_loss_metric.reset()
        self.train_acc_metric.reset()
        if self.val_loader is not None:
            self.val_loss_metric.reset()
            self.val_acc_metric.reset()
        if self.averaged_model is not None:
            self.avg_model_loss_metric.reset()
            self.avg_model_acc_metric.reset()

    def _epoch_str(self, epoch: int, epoch_time: float):
        s = f"Epoch {epoch} "
        s += f"| Train loss: {self.train_loss_metric.compute():.3f} "
        s += f"| Train acc: {self.train_acc_metric.compute():.3f} "
        if self.val_loader is not None:
            s += f"| Val loss: {self.val_loss_metric.compute():.3f} "
            s += f"| Val acc: {self.val_acc_metric.compute():.3f} "
            if self.averaged_model is not None:
                s += (
                    f"| Avg model val loss: {self.avg_model_loss_metric.compute():.3f} "
                )
                s += f"| Avg model val acc: {self.avg_model_acc_metric.compute():.3f} "
                s += f"| Weight diff: {self.weight_diff:.3f} "
        s += f"| Epoch time: {epoch_time:.1f}s"

        return s

    def _write_to_tb(self, epoch):
        self.writer.add_scalar("Loss/train", self.train_loss_metric.compute(), epoch)
        self.writer.add_scalar("Accuracy/train", self.train_acc_metric.compute(), epoch)

        if self.val_loader is not None:
            self.writer.add_scalar("Loss/val", self.val_loss_metric.compute(), epoch)
            self.writer.add_scalar("Accuracy/val", self.val_acc_metric.compute(), epoch)

            if self.averaged_model is not None:
                self.writer.add_scalar(
                    "Loss/averaged_val", self.avg_model_loss_metric.compute(), epoch
                )
                self.writer.add_scalar(
                    "Accuracy/averaged_val", self.avg_model_acc_metric.compute(), epoch
                )
                self.writer.add_scalar(
                    "Model/model_weight_norm", weight_norm(self.model), epoch
                )
                self.writer.add_scalar(
                    "Model/avg_model_weight_norm",
                    weight_norm(self.averaged_model.module),
                    epoch,
                )
                self.writer.add_scalar(
                    "Model/weight_diff_norm", self.weight_diff, epoch
                )
                self.writer.add_scalar(
                    "Model/lr", self.scheduler.get_last_lr()[0], epoch
                )

    def _save_model(self, path, epoch):
        obj = {
            "epoch": epoch + 1,
            "optimizer": self.optimizer.state_dict(),
            "model": self.model.state_dict(),
            "scheduler": self.scheduler.state_dict()
            if self.scheduler is not None
            else None,
        }
        torch.save(obj, os.path.join(self.save_path, path))

    def _save_averaged_model(self, path, epoch):
        obj = {
            "epoch": epoch + 1,
            "model": self.averaged_model.module.state_dict(),
            "decay": self.averaged_model.decay,
        }
        torch.save(obj, os.path.join(self.save_path, path))

    def _load_from_checkpoint(self, checkpoint_path: str) -> None:
        checkpoint = torch.load(checkpoint_path, map_location=self.device)
        self.model.load_state_dict(checkpoint["model"])
        self.optimizer.load_state_dict(checkpoint["optimizer"])

        self.start_epoch = checkpoint["epoch"]

        if self.scheduler:
            self.scheduler.load_state_dict(checkpoint["scheduler"])

        if self.start_epoch > self.epochs:
            raise ValueError("Starting epoch is larger than total epochs")

        self.logger.info(f"Checkpoint loaded, resuming from epoch {self.start_epoch}")
