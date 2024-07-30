import argparse
from   PIL import ImageFile
import torch
import torch.nn.functional as F
import torch.utils.data
import torchvision
from   torchvision.transforms import v2
import lightning.pytorch as pl
from   lightning.pytorch.loggers import TensorBoardLogger
from   lightning.pytorch.callbacks import ModelCheckpoint, LearningRateMonitor
import matplotlib.pyplot as plt
from   models import *
ImageFile.LOAD_TRUNCATED_IMAGES = True

parser = argparse.ArgumentParser()
parser.add_argument("--nepochs",      type=int,   default=100,    help="Number of epochs")
parser.add_argument("--batchsize",    type=int,   default=32,     help="Batch size")
parser.add_argument("--nwarmup",      type=int,   default=1000,   help="number of warmup steps")
parser.add_argument("--lr",           type=float, default=0.001,  help="Initial learning rate")
parser.add_argument("--trainRoot",    type=str,   required=True,  help="Root folder of training directory")
parser.add_argument("--trainAnn",     type=str,   required=True,  help="Training annotations file")
parser.add_argument("--valRoot",      type=str,   required=True,  help="Root folder of validation directory")
parser.add_argument("--valAnn",       type=str,   required=True,  help="Validation annotations file")
parser.add_argument("--nworkers",     type=int,   default=0,      help="Number of data workers. If 0, set to mp.cpu_count()/2")
args = parser.parse_args()
args.nworkers = torch.multiprocessing.cpu_count() // 2 if args.nworkers == 0 else args.nworkers

class CocoWrapper(torch.utils.data.Dataset):
    def __init__(self, root, annFile, transforms=[]):
        super().__init__()
        transforms   = v2.Compose([v2.ToImage(), *transforms, v2.ToDtype(torch.float32, scale=True)])
        dataset      = torchvision.datasets.CocoDetection(root, annFile, transforms=transforms)
        cat_ids      = dataset.coco.getCatIds()
        cats         = dataset.coco.loadCats(cat_ids)
        self.names   = [cat["name"] for cat in cats]
        self.ids     = {cat: id for id, cat in enumerate(cat_ids)}
        self.dataset = torchvision.datasets.wrap_dataset_for_transforms_v2(dataset, target_keys=["boxes", "labels"])
        
    def __len__(self):
        return len(self.dataset)
    
    def __getitem__(self, index):
        img, target = self.dataset[index]
        classes     = torch.tensor([self.ids[i.item()] for i in target['labels']]).unsqueeze(-1) if 'labels' in target else torch.zeros(0,1)
        boxes       = target['boxes'] if 'boxes' in target else torch.zeros(0,4)
        target      = torch.cat([boxes,classes], -1)
        return img, target

def CocoCollator(batch):
    imgs, targets   = zip(*batch)
    N               = max(t.shape[0] for t in targets)
    targets         = [F.pad(t, (0,0,0,N-t.shape[0]), value=-1) for t in targets]
    H               = max(x.shape[1] for x in imgs)
    W               = max(x.shape[2] for x in imgs)
    imgs            = [F.pad(x, (0,W-x.shape[2],0,H-x.shape[1]), value=0) for x in imgs]
    imgs            = torch.stack(imgs, 0)
    targets         = torch.stack(targets, 0)
    return imgs, targets

def createOptimizer(self: torch.nn.Module, momentum=0.9, lr=0.001, decay=0.0001):
    bn = tuple(v for k, v in nn.__dict__.items() if "Norm" in k)  # normalization layers
    g  = [], [], []
    for module_name, module in self.named_modules():
        for param_name, param in module.named_parameters(recurse=False):
            fullname = f"{module_name}.{param_name}" if module_name else param_name
            if "bias" in fullname:  
                g[2].append(param) # bias (no decay)
            elif isinstance(module, bn):  
                g[1].append(param) # weight (no decay)
            else:  
                g[0].append(param) # weight (with decay)
    num_non_decayed_biases  = sum(p.numel() for p in g[2])
    num_non_decayed_weights = sum(p.numel() for p in g[1])
    num_decayed_weights     = sum(p.numel() for p in g[0])
    print(f"num non-decayed biases  : {len(g[2])}, with {num_non_decayed_biases} parameters")
    print(f"num non-decayed weights : {len(g[1])}, with {num_non_decayed_weights} parameters")
    print(f"num decayed weights     : {len(g[0])}, with {num_decayed_weights} parameters")
    assert num_non_decayed_biases + num_non_decayed_weights + num_decayed_weights == sum(p.numel() for p in self.parameters() if p.requires_grad)
    optimizer = torch.optim.SGD(g[2], lr=lr, momentum=momentum, nesterov=True)
    # optimizer = torch.optim.AdamW(g[2], lr=lr, betas=(momentum, 0.999), fused=True)
    optimizer.add_param_group({"params": g[0], "weight_decay": decay})  # add g0 with weight_decay
    optimizer.add_param_group({"params": g[1], "weight_decay": 0.0})  # add g1 (BatchNorm2d weights)
    return optimizer

class LitModule(pl.LightningModule):
    def __init__(self, net, nc, nsteps):
        super().__init__()
        self.net = net
        self.nc  = nc
        self.nsteps = nsteps

    def training_step(self, batch, batch_idx):
        return self.step(batch, batch_idx, self.trainer.num_training_batches, is_training=True)

    def validation_step(self, batch, batch_idx):
        self.step(batch, batch_idx, self.trainer.num_val_batches[0], is_training=False)

    def step(self, batch, batch_idx, nbatches, is_training):
        imgs, targets = batch
        preds, losses = self.net(imgs, targets)
        loss          = 7.5 * losses['iou'] + 0.5 * losses['cls'] + 0.5 * losses['obj']
        # loss          = 7.5 * losses['iou'] + 0.5 * losses['cls'] + 1.5 * losses['dfl']

        label = "train" if is_training else "val"
        self.log("loss/obj/"   + label, losses['obj'].item(),   logger=False, prog_bar=False, on_step=True)
        # self.log("loss/dfl/"   + label, losses['dfl'].item(),   logger=False, prog_bar=False, on_step=True)
        self.log("loss/cls/"   + label, losses['cls'].item(),   logger=False, prog_bar=False, on_step=True)
        self.log("loss/iou/"   + label, losses['iou'].item(),   logger=False, prog_bar=False, on_step=True)
        self.log("loss/sum/"   + label, loss.item(),            logger=False, prog_bar=True, on_step=True, on_epoch=True)

        if self.trainer.is_global_zero:
            summary     = self.logger.experiment
            epoch       = self.current_epoch
            totalBatch  = (epoch + batch_idx / nbatches) * 1000

            summary.add_scalars("loss/obj",   {label: losses['obj'].item()},   totalBatch)
            # summary.add_scalars("loss/dfl",   {label: losses['dfl'].item()},   totalBatch)
            summary.add_scalars("loss/cls",   {label: losses['cls'].item()},   totalBatch)
            summary.add_scalars("loss/iou",   {label: losses['iou'].item()},   totalBatch)
            summary.add_scalars("loss/sum",   {label: loss.item()},            totalBatch)

            if batch_idx % 50 == 0:
                with torch.no_grad():
                    nfeats   = preds.shape[-1]
                    has_obj  = (nfeats - 4 - self.nc) > 0
                    _, preds = nms(preds[0:1], 0.3, 0.5, has_obj)
                    img      = (imgs[0]*255).to(torch.uint8)
                    canvas   = torchvision.utils.draw_bounding_boxes(img, preds[:,:4], [COCO_NAMES[i] for i in preds[:, -self.nc:].argmax(-1).long()])
                    fig = plt.figure()
                    plt.imshow(canvas.permute(1,2,0).cpu())
                    summary.add_figure('preds/'+label, fig, totalBatch)
        self.trainer.strategy.barrier()
        return loss
    
    def configure_optimizers(self):
        optimizer = createOptimizer(self, lr=args.lr)
        scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, 
                                                        max_lr=[g['lr'] for g in optimizer.param_groups], 
                                                        total_steps=self.nsteps,
                                                        pct_start=args.nwarmup/self.nsteps)
        return {'optimizer': optimizer, 'lr_scheduler': {'scheduler': scheduler, 'interval': "step", "frequency": 1}}

torch.set_float32_matmul_precision('medium')

transforms = [
    v2.RandomPhotometricDistort(p=0.7),
    v2.RandomGrayscale(p=0.3),
    v2.RandomPosterize(bits=4, p=0.7),
    v2.RandomHorizontalFlip(p=0.7),
    v2.RandomPerspective(distortion_scale=0.6, p=0.5),
    v2.Resize((640,640), antialias=True)
]
                                   
trainset    = CocoWrapper(args.trainRoot, args.trainAnn, transforms=transforms)
valset      = CocoWrapper(args.valRoot,   args.valAnn,   transforms=[v2.Resize((416,416), antialias=True)])
nclasses    = len(valset.names)
trainLoader = torch.utils.data.DataLoader(trainset, batch_size=args.batchsize, shuffle=True, collate_fn=CocoCollator, num_workers=args.nworkers)
valLoader   = torch.utils.data.DataLoader(valset, batch_size=args.batchsize, collate_fn=CocoCollator, num_workers=args.nworkers)
nsteps      = len(trainLoader) * args.nepochs

net = Yolov3(nclasses, spp=True)
init_batchnorms(net)
net = LitModule(net, nclasses, nsteps)

trainer = pl.Trainer(max_epochs=args.nepochs,
                     accelerator='gpu',
                     num_sanity_val_steps=0,
                     logger=TensorBoardLogger(save_dir="../runs", flush_secs=10),
                     callbacks= [LearningRateMonitor(logging_interval='step', log_momentum=True),
                                 ModelCheckpoint(filename='epoch_{epoch}-loss_{loss/sum/val_epoch}',
                                                 monitor='loss/sum/val_epoch', 
                                                 auto_insert_metric_name=False)])

trainer.fit(model=net, train_dataloaders=trainLoader, val_dataloaders=valLoader)