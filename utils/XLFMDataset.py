from json import load
import torch
from torch.utils import data
import torch.nn.functional as F
import csv
import glob
from PIL import Image
from torchvision.transforms import ToTensor
import numpy as np
from tifffile import imread
from tqdm import tqdm
import multipagetiff as mtif

import utils.pytorch_shot_noise as pytorch_shot_noise
from utils.misc_utils import *

def get_lenslet_centers(filename):
    x,y = [], []
    with open(filename,'r') as f:
        reader = csv.reader(f,delimiter='\t')
        for row in reader:
            x.append(int(row[0]))
            y.append(int(row[1]))
    lenslet_coords = torch.cat((torch.IntTensor(x).unsqueeze(1),torch.IntTensor(y).unsqueeze(1)),1)
    return lenslet_coords

class XLFMDatasetFull(data.Dataset):
    def __init__(self, data_path, lenslet_coords_path, subimage_shape, img_shape, images_to_use=None, lenslets_offset=50, n_depths_to_fill=120, border_blanking=10,
     load_vols=True, load_sparse=False, temporal_shifts=[0,1,2], use_random_shifts=False, maxWorkers=10):
        # Load lenslets coordinates
        self.lenslet_coords = get_lenslet_centers(lenslet_coords_path) + torch.tensor(lenslets_offset)
        self.n_lenslets = self.lenslet_coords.shape[0]
        self.images_to_use = images_to_use
        self.data_path = data_path
        self.load_vols = load_vols
        self.load_sparse = load_sparse
        self.temporal_shifts = temporal_shifts
        self.n_frames = len(temporal_shifts)
        self.use_random_shifts = use_random_shifts
        self.vol_type = torch.float16

        self.img_shape = img_shape
        self.subimage_shape = subimage_shape
        self.half_subimg_shape = [self.subimage_shape[0]//2,self.subimage_shape[1]//2]

        # Tiff images are stored in single tiff stack
        # Volumes are stored in individual tiff stacks
        imgs_path = data_path + '/XLFM_image/XLFM_image_stack.tif'
        imgs_path_sparse = data_path + '/XLFM_image/XLFM_image_stack_S.tif'
        vols_path = data_path + '/XLFM_stack/*.tif'

        self.img_dataset = imread(imgs_path, maxworkers=maxWorkers)
        n_frames,h,w = self.img_dataset.shape

        if self.load_sparse:
            try:
                self.img_dataset_sparse = imread(imgs_path_sparse, maxworkers=maxWorkers)
            except:
                self.load_sparse = False
                print('Dataset error: Sparse dir XLFM_image/XLFM_image_stack_S.tif not found')

        if images_to_use is None:
            images_to_use = list(range(n_frames))
        self.n_images = min(len(images_to_use), n_frames)

        n_images_to_load = max(images_to_use)+max(temporal_shifts) + 1

        self.all_files = sorted(glob.glob(vols_path))
        if len(self.all_files)>0 and load_vols:
            self.all_files = [sorted(glob.glob(vols_path))[images_to_use[i]] for i in range(self.n_images)]

        if self.load_sparse:
            vols_path_sparse = data_path + '/XLFM_stack_S/*.tif'
        if load_vols:
            if self.load_sparse:
                self.all_files= [sorted(glob.glob(vols_path_sparse))[images_to_use[i]] for i in range(self.n_images)]
            # read single volume
            currVol = self.read_tiff_stack(self.all_files[0])
            odd_size = [currVol.shape[0], currVol.shape[1]]
            # odd_size = [int(n - (1 if (n%2 == 0) else 0)) for n in odd_size]
            # currVol = currVol[0:odd_size[0],0:odd_size[1]]
            half_volume_shape = [odd_size[0]//2,odd_size[1]//2]
            self.volStart = [currVol.shape[0]//2-half_volume_shape[0], currVol.shape[1]//2-half_volume_shape[1]]
            self.volEnd = [odd_size[n] + self.volStart[n] for n in range(len(self.volStart))]
            self.vols = torch.zeros(n_images_to_load, n_depths_to_fill, odd_size[0], odd_size[1], dtype=self.vol_type)
            
        else:
            odd_size = self.subimage_shape
            self.vols = 255*torch.ones(1)
        
        # Create image storage
        self.stacked_views = torch.zeros(n_images_to_load, self.img_shape[0], self.img_shape[1],dtype=torch.float16)
        
        if self.load_sparse:
            stacked_views_sparse = self.stacked_views.clone()

        for nImg in range(n_images_to_load):

            # Load the images indicated from the user
            curr_img = nImg#images_to_use[nImg]

            if load_vols:
                currVol = self.read_tiff_stack(self.all_files[curr_img])
                assert not torch.isinf(currVol).any()
                if border_blanking>0:
                    currVol[:border_blanking,...] = 0
                    currVol[-border_blanking:,...] = 0
                    currVol[:,:border_blanking,...] = 0
                    currVol[:,-border_blanking:,...] = 0
                    currVol[:,:,:border_blanking] = 0
                    currVol[:,:,-border_blanking:] = 0
                self.vols[nImg,:currVol.shape[2],:,:] = currVol.permute(2,0,1)\
                    [:,self.volStart[0]:self.volEnd[0],self.volStart[1]:self.volEnd[1]]

            
            image = torch.from_numpy(np.array(self.img_dataset[curr_img,:,:]).astype(np.float16)).type(torch.float16)

            image = self.pad_img_to_min(image)
            self.stacked_views[nImg,...] = center_crop(image.unsqueeze(0).unsqueeze(0), self.img_shape)[0,0,...]
            
            if self.load_sparse:
                image = torch.from_numpy(np.array(self.img_dataset_sparse[curr_img,:,:]).astype(np.float16)).type(torch.float16)
                image = self.pad_img_to_min(image)
                stacked_views_sparse[nImg,...] = image

        if self.load_sparse:
            self.stacked_views = torch.cat((self.stacked_views.unsqueeze(-1), stacked_views_sparse.unsqueeze(-1)), dim=3)

        print('Loaded ' + str(self.n_images))  

    def __len__(self):
        'Denotes the total number of samples'
        return self.n_images

    def get_n_depths(self):
        return self.vols.shape[1]
    
    def get_n_temporal_frames(self):
        return len(self.temporal_shifts)

    def get_max(self):
        'Get max intensity from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().max().type(self.stacked_views.type()),\
                    self.stacked_views[...,1].float().max().type(self.stacked_views.type()),\
                    self.vols.float().max().type(self.vols.type())
        else:
            return self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.vols.float().max().type(self.vols.type())

    def get_statistics(self):
        'Get mean and standard deviation from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().mean().type(self.stacked_views.type()), self.stacked_views[...,0].float().std().type(self.stacked_views.type()), \
                    self.stacked_views[...,1].float().mean().type(self.stacked_views.type()), self.stacked_views[...,1].float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())
        else:
            return  self.stacked_views.float().mean().type(self.stacked_views.type()), self.stacked_views.float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())

    def standarize(self, stats=None):
        mean_imgs, std_imgs, mean_imgs_s, std_imgs_s, mean_vols, std_vols = stats
        if self.load_sparse:
            self.stacked_views[...,0] = (self.stacked_views[...,0]-mean_imgs) / std_imgs
            self.stacked_views[...,1] = (self.stacked_views[...,1]-mean_imgs_s) / std_imgs_s
        else:
            self.stacked_views[...] = (self.stacked_views[...]-mean_imgs) / std_imgs
        self.vols = (self.vols-mean_vols) / std_vols

    def len_lenslets(self):
        'Denotes the total number of lenslets'
        return self.n_lenslets
    def get_lenslets_coords(self):
        'Returns the 2D coordinates of the lenslets'
        return self.lenslet_coords

    def pad_img_to_min(self,image):
        min_size = min(image.shape[-2:])
        img_pad = [min_size-image.shape[-1], min_size-image.shape[-2]]
        img_pad = [img_pad[0]//2, img_pad[0]//2, img_pad[1],img_pad[1]]
        image = F.pad(image.unsqueeze(0).unsqueeze(0), img_pad)[0,0]
        return image

    def __getitem__(self, index):
        n_frames = self.get_n_temporal_frames()
        new_index = self.images_to_use[index]
        
        temporal_shifts_ixs = self.temporal_shifts
        # if self.use_random_shifts:
        #     temporal_shifts_ixs = torch.randint(0, self.n_images-1,[3]).numpy()
        #     newIndex = 0

        indices = [new_index + temporal_shifts_ixs[i] for i in range(n_frames)]
        
        views_out = self.stacked_views[indices,...]
        if self.load_vols is False:
            return views_out,0
        vol_out = self.vols[index,...]
        
        return views_out,vol_out

    
    def get_voltages_from_volume(self, vol, neuron):
        index = (self.seg_vol.long() == neuron).nonzero()
        
        return vol[index]

    @staticmethod
    def extract_views(image, lenslet_coords, subimage_shape, debug=False):
        half_subimg_shape = [subimage_shape[0]//2,subimage_shape[1]//2]
        n_lenslets = lenslet_coords.shape[0]
        stacked_views = torch.zeros(size=[image.shape[0], image.shape[1], n_lenslets, subimage_shape[0], subimage_shape[1]], device=image.device, dtype=image.dtype)
        
        if debug:
            debug_image = image.detach().clone()
            max_img = image.float().cpu().max()
        for nLens in range(n_lenslets):
            # Fetch coordinates
            currCoords = lenslet_coords[nLens,:]
            if debug:
                debug_image[:,:,currCoords[0]-2:currCoords[0]+2,currCoords[1]-2:currCoords[1]+2] = max_img
            # Grab patches
            lower_bounds = [currCoords[0]-half_subimg_shape[0], currCoords[1]-half_subimg_shape[1]]
            lower_bounds = [max(lower_bounds[kk],0) for kk in range(2)]
            currPatch = image[:,:,lower_bounds[0] : currCoords[0]+half_subimg_shape[0], lower_bounds[1] : currCoords[1]+half_subimg_shape[1]]
            stacked_views[:,:,nLens,-currPatch.shape[2]:,-currPatch.shape[3]:] = currPatch
        
        if debug:
            import matplotlib.pyplot as plt
            plt.imshow(debug_image[0,0,...].float().cpu().detach().numpy())
            plt.show()
        return stacked_views
        
    @staticmethod
    def read_tiff_stack(filename, out_datatype=torch.float16):
        tiffarray = mtif.read_stack(filename, units='voxels')
        try:
            max_val = torch.iinfo(out_datatype).max
        except:
            max_val = torch.finfo(out_datatype).max
        out = np.clip(tiffarray.raw_images, 0, max_val)
        return torch.from_numpy(out).permute(1,2,0).type(out_datatype)

    def add_random_shot_noise_to_dataset(self, signal_power_range=[32**2,32**2]):
        for nImg in range(self.stacked_views.shape[0]):
            signal_power = (signal_power_range[0] + (signal_power_range[1]-signal_power_range[0]) * torch.rand(1)).item()
            
            curr_img_stack = self.stacked_views[nImg,...].float()
            curr_max = curr_img_stack.max()
            curr_img_stack = signal_power * curr_img_stack / curr_max
                
            for kk in range(curr_img_stack.shape[0]):
                curr_img_stack[kk,...] = pytorch_shot_noise.add_camera_noise(curr_img_stack[kk,...])
            curr_img_stack = curr_max * curr_img_stack.float() / signal_power
            self.stacked_views[nImg,...] = curr_img_stack
        print("Added noise to " + str(self.stacked_views.shape[0]) + " images.")






class XLFMDatasetFullRegistration(data.Dataset):
    def __init__(self, data_path, lenslet_coords_path, subimage_shape, img_shape, images_to_use=None, lenslets_offset=50, n_depths_to_fill=120, border_blanking=10,
     load_vols=True, load_sparse=False, temporal_shifts=[0,1,2], use_random_shifts=False, maxWorkers=10):
        # Load lenslets coordinates
        self.lenslet_coords = get_lenslet_centers(lenslet_coords_path) + torch.tensor(lenslets_offset)
        self.n_lenslets = self.lenslet_coords.shape[0]
        self.data_path = data_path
        self.load_vols = load_vols
        self.load_sparse = load_sparse
        self.temporal_shifts = temporal_shifts
        self.use_random_shifts = use_random_shifts
        self.vol_type = torch.float16

        self.img_shape = img_shape
        self.subimage_shape = subimage_shape
        self.half_subimg_shape = [self.subimage_shape[0]//2,self.subimage_shape[1]//2]

        # Tiff images are stored in single tiff stack
        # Volumes are stored in individual tiff stacks
        imgs_path = data_path + '/XLFM_image/XLFM_image_stack.tif'
        imgs_path_sparse = data_path + '/XLFM_image/XLFM_image_stack_S.tif'
        vols_path = data_path + '/XLFM_stack/*.tif'

        # dataset = Image.open(imgs_path)
        self.img_dataset = imread(imgs_path, maxworkers=maxWorkers)
        n_frames,h,w = np.shape(self.img_dataset)

        if self.load_sparse:
            try:
                self.img_dataset_sparse = imread(imgs_path_sparse, maxworkers=maxWorkers)
            except:
                self.load_sparse = False
                print('Dataset error: Sparse dir XLFM_image/XLFM_image_stack_S.tif not found')

        if images_to_use is None:
            images_to_use = list(range(n_frames))
        self.n_images = min(len(images_to_use), n_frames)

        self.all_files = sorted(glob.glob(vols_path))
        if len(self.all_files)>0 and load_vols:
            self.all_files = [sorted(glob.glob(vols_path))[images_to_use[i]] for i in range(self.n_images)]

        if self.load_sparse:
            vols_path_sparse = data_path + '/XLFM_stack_S/*.tif'
        if load_vols:
            if self.load_sparse:
                self.all_files= [sorted(glob.glob(vols_path_sparse))[images_to_use[i]] for i in range(self.n_images)]
            # read single volume
            currVol = self.read_tiff_stack(self.all_files[0])
            odd_size = [currVol.shape[0], currVol.shape[1]]
            # odd_size = [int(n - (1 if (n%2 == 0) else 0)) for n in odd_size]
            # currVol = currVol[0:odd_size[0],0:odd_size[1]]
            half_volume_shape = [odd_size[0]//2,odd_size[1]//2]
            self.volStart = [currVol.shape[0]//2-half_volume_shape[0], currVol.shape[1]//2-half_volume_shape[1]]
            self.volEnd = [odd_size[n] + self.volStart[n] for n in range(len(self.volStart))]
            self.vols = torch.zeros(self.n_images, n_depths_to_fill, odd_size[0], odd_size[1], dtype=self.vol_type)
            
        else:
            odd_size = self.subimage_shape
            self.vols = 255*torch.ones(1)
        
        # Create image storage
        self.stacked_views = torch.zeros(self.n_images, self.img_shape[0], self.img_shape[1],dtype=torch.float16)
        
        if self.load_sparse:
            stacked_views_sparse = self.stacked_views.clone()

        for nImg in range(self.n_images):

            # Load the images indicated from the user
            curr_img = images_to_use[nImg]

            if load_vols:
                currVol = self.read_tiff_stack(self.all_files[curr_img])
                assert not torch.isinf(currVol).any()
                if border_blanking>0:
                    currVol[:border_blanking,...] = 0
                    currVol[-border_blanking:,...] = 0
                    currVol[:,:border_blanking,...] = 0
                    currVol[:,-border_blanking:,...] = 0
                    currVol[:,:,:border_blanking] = 0
                    currVol[:,:,-border_blanking:] = 0
                self.vols[nImg,:currVol.shape[2],:,:] = currVol.permute(2,0,1)\
                    [:,self.volStart[0]:self.volEnd[0],self.volStart[1]:self.volEnd[1]]

            
            image = torch.from_numpy(np.array(self.img_dataset[curr_img,:,:]).astype(np.float16)).type(torch.float16)

            image = self.pad_img_to_min(image)
            self.stacked_views[nImg,...] = center_crop(image.unsqueeze(0).unsqueeze(0), self.img_shape)[0,0,...]
            
            if self.load_sparse:
                image = torch.from_numpy(np.array(self.img_dataset_sparse[curr_img,:,:]).astype(np.float16)).type(torch.float16)
                image = self.pad_img_to_min(image)
                stacked_views_sparse[nImg,...] = image

        if self.load_sparse:
            self.stacked_views = torch.cat((self.stacked_views.unsqueeze(-1), stacked_views_sparse.unsqueeze(-1)), dim=3)

        print('Loaded ' + str(self.n_images))  

    def __len__(self):
        'Denotes the total number of samples'
        if self.use_random_shifts:
            return self.n_images
        return self.n_images-np.max(self.temporal_shifts)

    def get_n_depths(self):
        return self.vols.shape[1]
    
    def get_n_temporal_frames(self):
        return len(self.temporal_shifts)

    def get_max(self):
        'Get max intensity from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().max().type(self.stacked_views.type()),\
                    self.stacked_views[...,1].float().max().type(self.stacked_views.type()),\
                    self.vols.float().max().type(self.vols.type())
        else:
            return self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.vols.float().max().type(self.vols.type())

    def get_statistics(self):
        'Get mean and standard deviation from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().mean().type(self.stacked_views.type()), self.stacked_views[...,0].float().std().type(self.stacked_views.type()), \
                    self.stacked_views[...,1].float().mean().type(self.stacked_views.type()), self.stacked_views[...,1].float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())
        else:
            return  self.stacked_views.float().mean().type(self.stacked_views.type()), self.stacked_views.float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())

    def standarize(self, stats=None):
        mean_imgs, std_imgs, mean_imgs_s, std_imgs_s, mean_vols, std_vols = stats
        if self.load_sparse:
            self.stacked_views[...,0] = (self.stacked_views[...,0]-mean_imgs) / std_imgs
            self.stacked_views[...,1] = (self.stacked_views[...,1]-mean_imgs_s) / std_imgs_s
        else:
            self.stacked_views[...] = (self.stacked_views[...]-mean_imgs) / std_imgs
        self.vols = (self.vols-mean_vols) / std_vols

    def len_lenslets(self):
        'Denotes the total number of lenslets'
        return self.n_lenslets
    def get_lenslets_coords(self):
        'Returns the 2D coordinates of the lenslets'
        return self.lenslet_coords

    def pad_img_to_min(self,image):
        min_size = min(image.shape[-2:])
        img_pad = [min_size-image.shape[-1], min_size-image.shape[-2]]
        img_pad = [img_pad[0]//2, img_pad[0]//2, img_pad[1],img_pad[1]]
        image = F.pad(image.unsqueeze(0).unsqueeze(0), img_pad)[0,0]
        return image

    def __getitem__(self, index):
        n_frames = self.get_n_temporal_frames()
        newIndex = index
        
        temporal_shifts_ixs = list(range(n_frames))
        if len(self.temporal_shifts)>0 and not sorted(self.temporal_shifts) == list(range(min(self.temporal_shifts), max(self.temporal_shifts)+1)):
            temporal_shifts_ixs = self.temporal_shifts
            newIndex = index
            if self.use_random_shifts:
                temporal_shifts_ixs = torch.randint(0, self.n_images-1,[3]).numpy()
                newIndex = 0
        img_index = newIndex

        indices = [img_index + temporal_shifts_ixs[i] for i in range(n_frames)]
        
        views_out = self.stacked_views[indices,...]
        if self.load_vols is False:
            return views_out,0
        vol_out1 = self.vols[index,...]
        vol_out2 = self.vols[torch.randint(0,self.__len__()),...]
        
        return views_out,vol_out2

    
    def get_voltages_from_volume(self, vol, neuron):
        index = (self.seg_vol.long() == neuron).nonzero()
        
        return vol[index]

    @staticmethod
    def extract_views(image, lenslet_coords, subimage_shape, debug=False):
        half_subimg_shape = [subimage_shape[0]//2,subimage_shape[1]//2]
        n_lenslets = lenslet_coords.shape[0]
        stacked_views = torch.zeros(size=[image.shape[0], image.shape[1], n_lenslets, subimage_shape[0], subimage_shape[1]], device=image.device, dtype=image.dtype)
        
        if debug:
            debug_image = image.detach().clone()
            max_img = image.float().cpu().max()
        for nLens in range(n_lenslets):
            # Fetch coordinates
            currCoords = lenslet_coords[nLens,:]
            if debug:
                debug_image[:,:,currCoords[0]-2:currCoords[0]+2,currCoords[1]-2:currCoords[1]+2] = max_img
            # Grab patches
            lower_bounds = [currCoords[0]-half_subimg_shape[0], currCoords[1]-half_subimg_shape[1]]
            lower_bounds = [max(lower_bounds[kk],0) for kk in range(2)]
            currPatch = image[:,:,lower_bounds[0] : currCoords[0]+half_subimg_shape[0], lower_bounds[1] : currCoords[1]+half_subimg_shape[1]]
            stacked_views[:,:,nLens,-currPatch.shape[2]:,-currPatch.shape[3]:] = currPatch
        
        if debug:
            import matplotlib.pyplot as plt
            plt.imshow(debug_image[0,0,...].float().cpu().detach().numpy())
            plt.show()
        return stacked_views
        
    @staticmethod
    def read_tiff_stack(filename, out_datatype=np.float16):
        try:
            max_val = np.iinfo(out_datatype).max
        except:
            max_val = np.finfo(out_datatype).max

        dataset = Image.open(filename)
        h,w = np.shape(dataset)
        tiffarray = np.zeros([h,w,dataset.n_frames], dtype=out_datatype)
        for i in range(dataset.n_frames):
            dataset.seek(i)
            img =  np.array(dataset)
            img = np.nan_to_num(img)
            # img[img>=max_val/2] = max_val/2
            tiffarray[:,:,i] = img.astype(out_datatype)
        return torch.from_numpy(tiffarray)

    def add_random_shot_noise_to_dataset(self, signal_power_range=[32**2,32**2]):
        for nImg in range(self.stacked_views.shape[0]):
            signal_power = (signal_power_range[0] + (signal_power_range[1]-signal_power_range[0]) * torch.rand(1)).item()
            
            curr_img_stack = self.stacked_views[nImg,...].float()
            curr_max = curr_img_stack.max()
            curr_img_stack = signal_power * curr_img_stack / curr_max
                
            for kk in range(curr_img_stack.shape[0]):
                curr_img_stack[kk,...] = pytorch_shot_noise.add_camera_noise(curr_img_stack[kk,...])
            curr_img_stack = curr_max * curr_img_stack.float() / signal_power
            self.stacked_views[nImg,...] = curr_img_stack
        print("Added noise to " + str(self.stacked_views.shape[0]) + " images.")




class XLFMDatasetVol(data.Dataset):
    def __init__(self, data_path, lenslet_coords_path, subimage_shape, img_shape, images_to_use=None, lenslets_offset=50, n_depths_to_fill=120, border_blanking=10,
     load_imgs=False, load_vols=True, load_sparse=False, temporal_shifts=[0,1,2], use_random_shifts=False, maxWorkers=10):
        # Load lenslets coordinates
        self.lenslet_coords = get_lenslet_centers(lenslet_coords_path) + torch.tensor(lenslets_offset)
        self.n_lenslets = self.lenslet_coords.shape[0]
        self.data_path = data_path
        self.load_imgs = load_imgs
        self.load_vols = load_vols
        self.load_sparse = load_sparse
        self.temporal_shifts = temporal_shifts
        self.use_random_shifts = use_random_shifts
        self.vol_type = torch.float16

        self.img_shape = img_shape
        self.subimage_shape = subimage_shape
        self.half_subimg_shape = [self.subimage_shape[0]//2,self.subimage_shape[1]//2]

        # Tiff images are stored in single tiff stack
        # Volumes are stored in individual tiff stacks
        imgs_path = data_path + '/XLFM_image/XLFM_image_stack.tif'
        imgs_path_sparse = data_path + '/XLFM_image/XLFM_image_stack_S.tif'
        vols_path = data_path + '/XLFM_stack/*.tif'

        # dataset = Image.open(imgs_path)
        if load_imgs:
            self.img_dataset = imread(imgs_path, maxworkers=maxWorkers)
            n_frames,h,w = np.shape(self.img_dataset)
            if self.load_sparse:
                try:
                    self.img_dataset_sparse = imread(imgs_path_sparse, maxworkers=maxWorkers)
                except:
                    self.load_sparse = False
                    print('Dataset error: Sparse dir XLFM_image/XLFM_image_stack_S.tif not found')
        else:
            n_frames = len(images_to_use)

        

        if images_to_use is None:
            images_to_use = list(range(n_frames))
        self.n_images = min(len(images_to_use), n_frames)

        self.all_files = sorted(glob.glob(vols_path))
        if len(self.all_files)>0 and load_vols:
            self.all_files = [sorted(glob.glob(vols_path))[images_to_use[i]] for i in range(self.n_images)]

        if self.load_sparse:
            vols_path_sparse = data_path + '/XLFM_stack_S/*.tif'
        if load_vols:
            if self.load_sparse:
                self.all_files= [sorted(glob.glob(vols_path_sparse))[images_to_use[i]] for i in range(self.n_images)]
            # read single volume
            currVol = self.read_tiff_stack(self.all_files[0])
            odd_size = [currVol.shape[0], currVol.shape[1]]
            # odd_size = [int(n - (1 if (n%2 == 0) else 0)) for n in odd_size]
            # currVol = currVol[0:odd_size[0],0:odd_size[1]]
            half_volume_shape = [odd_size[0]//2,odd_size[1]//2]
            self.volStart = [currVol.shape[0]//2-half_volume_shape[0], currVol.shape[1]//2-half_volume_shape[1]]
            self.volEnd = [odd_size[n] + self.volStart[n] for n in range(len(self.volStart))]
            self.vols = torch.zeros(self.n_images, n_depths_to_fill, odd_size[0], odd_size[1], dtype=self.vol_type)
            
        else:
            odd_size = self.subimage_shape
            self.vols = 255*torch.ones(1)
        
        if load_imgs:
            # Create image storage
            self.stacked_views = torch.zeros(self.n_images, self.img_shape[0], self.img_shape[1],dtype=torch.float16)
        else:
            self.stacked_views = torch.ones([1])
        if self.load_sparse:
            stacked_views_sparse = self.stacked_views.clone()

        for nImg in tqdm (range(self.n_images), desc="Loading Img..."):
            # Load the images indicated from the user
            curr_img = images_to_use[nImg]

            if load_vols:
                currVol = self.read_tiff_stack(self.all_files[curr_img])
                assert not torch.isinf(currVol).any()
                if border_blanking>0:
                    currVol[:border_blanking,...] = 0
                    currVol[-border_blanking:,...] = 0
                    currVol[:,:border_blanking,...] = 0
                    currVol[:,-border_blanking:,...] = 0
                    currVol[:,:,:border_blanking] = 0
                    currVol[:,:,-border_blanking:] = 0
                self.vols[nImg,:currVol.shape[2],:,:] = currVol.permute(2,0,1)\
                    [:,self.volStart[0]:self.volEnd[0],self.volStart[1]:self.volEnd[1]]

            if load_imgs:
                image = torch.from_numpy(np.array(self.img_dataset[curr_img,:,:]).astype(np.float16)).type(torch.float16)
                image = self.pad_img_to_min(image)
                self.stacked_views[nImg,...] = center_crop(image.unsqueeze(0).unsqueeze(0), self.img_shape)[0,0,...]
                
                if self.load_sparse:
                    image = torch.from_numpy(np.array(self.img_dataset_sparse[curr_img,:,:]).astype(np.float16)).type(torch.float16)
                    image = self.pad_img_to_min(image)
                    stacked_views_sparse[nImg,...] = image

        if load_imgs and self.load_sparse:
            self.stacked_views = torch.cat((self.stacked_views.unsqueeze(-1), stacked_views_sparse.unsqueeze(-1)), dim=3)

        print('Loaded ' + str(self.n_images))  

    def __len__(self):
        'Denotes the total number of samples'
        if self.use_random_shifts:
            return self.n_images
        return self.n_images-np.max(self.temporal_shifts)

    def get_n_depths(self):
        return self.vols.shape[1]
    
    def get_n_temporal_frames(self):
        return len(self.temporal_shifts)

    def get_max(self):
        'Get max intensity from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().max().type(self.stacked_views.type()),\
                    self.stacked_views[...,1].float().max().type(self.stacked_views.type()),\
                    self.vols.float().max().type(self.vols.type())
        else:
            return self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.stacked_views.float().max().type(self.stacked_views.type()),\
                self.vols.float().max().type(self.vols.type())

    def get_statistics(self):
        'Get mean and standard deviation from volumes and images for normalization'
        if self.load_sparse:
            return  self.stacked_views[...,0].float().mean().type(self.stacked_views.type()), self.stacked_views[...,0].float().std().type(self.stacked_views.type()), \
                    self.stacked_views[...,1].float().mean().type(self.stacked_views.type()), self.stacked_views[...,1].float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())
        else:
            return  self.stacked_views.float().mean().type(self.stacked_views.type()), self.stacked_views.float().std().type(self.stacked_views.type()), \
                    self.vols.float().mean().type(self.vols.type()), self.vols.float().std().type(self.vols.type())

    def standarize(self, stats=None):
        mean_imgs, std_imgs, mean_imgs_s, std_imgs_s, mean_vols, std_vols = stats
        if self.load_sparse:
            self.stacked_views[...,0] = (self.stacked_views[...,0]-mean_imgs) / std_imgs
            self.stacked_views[...,1] = (self.stacked_views[...,1]-mean_imgs_s) / std_imgs_s
        else:
            self.stacked_views[...] = (self.stacked_views[...]-mean_imgs) / std_imgs
        self.vols = (self.vols-mean_vols) / std_vols

    def len_lenslets(self):
        'Denotes the total number of lenslets'
        return self.n_lenslets
    def get_lenslets_coords(self):
        'Returns the 2D coordinates of the lenslets'
        return self.lenslet_coords

    def pad_img_to_min(self,image):
        min_size = min(image.shape[-2:])
        img_pad = [min_size-image.shape[-1], min_size-image.shape[-2]]
        img_pad = [img_pad[0]//2, img_pad[0]//2, img_pad[1],img_pad[1]]
        image = F.pad(image.unsqueeze(0).unsqueeze(0), img_pad)[0,0]
        return image

    def __getitem__(self, index):
        n_frames = self.get_n_temporal_frames()
        newIndex = index
        
        temporal_shifts_ixs = list(range(n_frames))
        if len(self.temporal_shifts)>0 and not sorted(self.temporal_shifts) == list(range(min(self.temporal_shifts), max(self.temporal_shifts)+1)):
            temporal_shifts_ixs = self.temporal_shifts
            newIndex = index
            if self.use_random_shifts:
                temporal_shifts_ixs = torch.randint(0, self.n_images-1,[3]).numpy()
                newIndex = 0
        img_index = newIndex

        indices = [img_index + temporal_shifts_ixs[i] for i in range(n_frames)]
        
        if self.load_imgs:
            views_out = self.stacked_views[indices,...]
        else:
            views_out = 0
        if self.load_vols:
            vol_out = self.vols[indices,...]
        else:
            vol_out = 0

        return views_out,vol_out

    
    def get_voltages_from_volume(self, vol, neuron):
        index = (self.seg_vol.long() == neuron).nonzero()
        
        return vol[index]

    @staticmethod
    def extract_views(image, lenslet_coords, subimage_shape, debug=False):
        half_subimg_shape = [subimage_shape[0]//2,subimage_shape[1]//2]
        n_lenslets = lenslet_coords.shape[0]
        stacked_views = torch.zeros(size=[image.shape[0], image.shape[1], n_lenslets, subimage_shape[0], subimage_shape[1]], device=image.device, dtype=image.dtype)
        
        if debug:
            debug_image = image.detach().clone()
            max_img = image.float().cpu().max()
        for nLens in range(n_lenslets):
            # Fetch coordinates
            currCoords = lenslet_coords[nLens,:]
            if debug:
                debug_image[:,:,currCoords[0]-2:currCoords[0]+2,currCoords[1]-2:currCoords[1]+2] = max_img
            # Grab patches
            lower_bounds = [currCoords[0]-half_subimg_shape[0], currCoords[1]-half_subimg_shape[1]]
            lower_bounds = [max(lower_bounds[kk],0) for kk in range(2)]
            currPatch = image[:,:,lower_bounds[0] : currCoords[0]+half_subimg_shape[0], lower_bounds[1] : currCoords[1]+half_subimg_shape[1]]
            stacked_views[:,:,nLens,-currPatch.shape[2]:,-currPatch.shape[3]:] = currPatch
        
        if debug:
            import matplotlib.pyplot as plt
            plt.imshow(debug_image[0,0,...].float().cpu().detach().numpy())
            plt.show()
        return stacked_views
        
    @staticmethod
    def read_tiff_stack(filename, out_datatype=torch.float16):
        tiffarray = mtif.read_stack(filename, units='voxels')
        try:
            max_val = torch.iinfo(out_datatype).max
        except:
            max_val = torch.finfo(out_datatype).max
        out = np.clip(tiffarray.raw_images, 0, max_val)
        return torch.from_numpy(out).permute(1,2,0).type(out_datatype)

    def add_random_shot_noise_to_dataset(self, signal_power_range=[32**2,32**2]):
        for nImg in range(self.stacked_views.shape[0]):
            signal_power = (signal_power_range[0] + (signal_power_range[1]-signal_power_range[0]) * torch.rand(1)).item()
            
            curr_img_stack = self.stacked_views[nImg,...].float()
            curr_max = curr_img_stack.max()
            curr_img_stack = signal_power * curr_img_stack / curr_max
                
            for kk in range(curr_img_stack.shape[0]):
                curr_img_stack[kk,...] = pytorch_shot_noise.add_camera_noise(curr_img_stack[kk,...])
            curr_img_stack = curr_max * curr_img_stack.float() / signal_power
            self.stacked_views[nImg,...] = curr_img_stack
        print("Added noise to " + str(self.stacked_views.shape[0]) + " images.")







