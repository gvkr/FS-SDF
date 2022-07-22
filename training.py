from __future__ import division
import src.meta_modules as meta;
from src.utils import get_exp, freeze
import importlib

training = importlib.import_module("if-net.models.training")
from src.train_fn import val_epoch, train_epoch
import torch.optim as optim
from src import models
import torch
from glob import glob
import numpy as np
import os
import re
class BatchedMetaTrainer(training.Trainer):

    def __init__(self, model, device, train_dataset, val_dataset, 
                 exp_name,fast_lr = 1e-4,outer_lr = 1e-4,  optimizer='Adam', 
                 matching_model = False, checkpoint = None,
                val_subset = None, val_batches = 15,
                pretrained_encoder = True,
                freeze_encoder = True,
                pretrained_decoder = False, 
                 **kwargs ):
        self.r_checkpoint = -1
        self.p_encoder = pretrained_encoder      
        self.p_decoder = pretrained_decoder 
        self.f_encoder = freeze_encoder   
        self.val_subset = val_subset
        self.val_batches = val_batches
        self.meta_model = model
        self.fast_lr = fast_lr
        self.lr = outer_lr
        super().__init__(  self.meta_model, device, train_dataset, val_dataset, exp_name, optimizer, **kwargs )
        self.target = torch.tensor(0.0, device = self.device )
        self.checkpoint = checkpoint
        
    def metatrain(self, 
                iterations, 
                fas, 
                pretrained_model, 
                train_subset = None, 
                lr_type='per_parameter',
                inner_loss = None,
                outer_loss =None):
        if self.p_encoder:
            self.meta_model.encoder = self.load_pretrained( self.meta_model.encoder,
                                                       pretrained_model,
                                                       branch = "encoder.").cuda()
            if self.f_encoder:
                self.meta_model.encoder = freeze(self.meta_model.encoder)
        if self.p_decoder:
            self.meta_model = self.load_pretrained( self.meta_model,
                                                       pretrained_model,
                                                       branch = "").cuda()
            if self.f_encoder:
                self.meta_model.encoder = freeze(self.meta_model.encoder)
        
        try:
            
            start = self.load_checkpoint()
            self.meta_model.decoder = meta.MetaSDF(self.meta_model.decoder,
                                               init_lr=self.fast_lr, 
                                               num_meta_steps = fas,
                                               loss =inner_loss,
                                               lr_type=lr_type,
                                               ).cuda()
            self.optimizer = optim.Adam( filter(lambda p: p.requires_grad ,self.meta_model.parameters()), lr=self.lr)
            print('New model')
        except:
            self.meta_model.decoder = meta.MetaSDF(self.meta_model.decoder,
                                               init_lr=self.fast_lr, 
                                               num_meta_steps = fas,
                                               loss =inner_loss,
                                               lr_type=lr_type,
                                               ).cuda()

            self.optimizer = optim.Adam( filter(lambda p: p.requires_grad ,self.meta_model.parameters()), lr=self.lr)
            start = self.load_checkpoint()

        
        train_loader = self.train_dataset.get_loader(train_subset)
        for epoch in range(start, iterations):
            
            print()
            print("Epoch:", epoch)
            print()
            #Meta validation
            if epoch % 1 == 0:
                self.save_checkpoint(epoch)
                val_loss = val_epoch(self.meta_model.encoder,
                                     self.meta_model.decoder,
                                     self.val_dataset,
                                    subset = self.val_subset,
                                    num_batches = self.val_batches,
                                    loss_fn = outer_loss)
                
                if self.val_min is None:
                    self.val_min = val_loss

                if val_loss < self.val_min:
                    self.val_min = val_loss
                    for path in glob(self.exp_path + 'val_min=*'):
                        os.remove(path)
                    np.save(self.exp_path + 'val_min={}'.format(epoch),[epoch,val_loss])
                self.writer.add_scalar('val loss batch avg', val_loss, epoch)
            #Meta Training 
            train_loss = train_epoch(self.meta_model.encoder, 
                                     self.meta_model.decoder, 
                                     train_loader,  
                                     self.optimizer,
                                     loss_fn= outer_loss)
            
            #self.writer.add_scalar('training loss last batch', task_error, epoch)
            self.writer.add_scalar('training loss batch avg', train_loss, epoch)
            
    def get_checkpoint_dict(self, model_name):
        exp             = get_exp(self.val_dataset, model_name)
        exp_path        = self.checkpoint_path.split('experiments')[0]+ 'experiments/{}/'.format( exp)
        checkpoint_path = exp_path + 'checkpoints/'.format( exp); print
        checkpoints     = glob(checkpoint_path+'/*')
        if len(checkpoints) == 0:
            print('No checkpoints found at {}'.format(self.checkpoint_path))
            return 0
        checkpoints = [os.path.splitext(os.path.basename(path))[0][17:] for path in checkpoints]
        checkpoints = np.array(checkpoints, dtype=int)
        checkpoints = np.sort(checkpoints)
        checkpoint  = checkpoints[-1] if self.checkpoint is None else self.checkpoint
        path = checkpoint_path + 'checkpoint_epoch_{}.tar'.format(checkpoint)
        print('Loaded checkpoint from: {}'.format(path))
        checkpoint_dict = torch.load(path)
        return checkpoint_dict
    def load_pretrained(self, model,  model_name, branch):
        if re.match('ShapeNetPoints_sdf_sep.*', model_name ) is not None and branch != 'encoder.':
            return self.batched_from_pretrained(model, model_name, self.checkpoint)
        checkpoint = self.get_checkpoint_dict(model_name)
        model.eval()
        state_dict_ = model.state_dict()

        keys = state_dict_.keys();
        state_dict_.update( {key: checkpoint['model_state_dict'][ branch + key] for key in keys })
        model.load_state_dict(state_dict_)
        return model

    def load_checkpoint(self):
        checkpoints = glob(self.checkpoint_path+'/*')
        if len(checkpoints) == 0:
            print('No checkpoints found at {}'.format(self.checkpoint_path))
            return 0
        checkpoints = [os.path.splitext(os.path.basename(path))[0][17:] for path in checkpoints]
        checkpoints = np.array(checkpoints, dtype=int)
        checkpoints = np.sort(checkpoints)
        path = self.checkpoint_path + 'checkpoint_epoch_{}.tar'.format(checkpoints[self.r_checkpoint])
        print('Loaded checkpoint from: {}'.format(path))
        checkpoint = torch.load(path)
        #print(checkpoint.get('model_state_dict'))
        self.meta_model.eval()
        self.meta_model.load_state_dict(checkpoint['model_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        epoch = checkpoint['epoch']
        return epoch

    def batched_from_pretrained(self, batched_model, model_name, checkpoint):
        w_to_fc = lambda x, y : y.squeeze(2) if x.split('.')[-1]=='weight'  else y
        exp = get_exp(self.val_dataset, model_name)
        path = f'experiments/{exp}/checkpoints/checkpoint_epoch_{checkpoint}.tar'
        
        assert re.match('ShapeNetPoints_sdf_sep.*', model_name ) is not None, "Please change the model name to a correspond to  ShapeNetPoints_sdf"
            
        sdf_model = models.ShapeNetPoints_sdf_sep()
        sdf_model.load_state_dict(torch.load(path)['model_state_dict'])
        batched_model.encoder = sdf_model.encoder
        stdict_conv =sdf_model.decoder.state_dict() 
        stdict =batched_model.decoder.state_dict() 
        mapping = {conv_key: key for conv_key, key in zip(stdict_conv, stdict) }
        new_stdict = {mapping[key]: w_to_fc(key, value) for key, value in stdict_conv.items() }
        batched_model.decoder.load_state_dict(new_stdict)
        return batched_model