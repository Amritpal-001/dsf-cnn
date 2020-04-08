import math

import cv2
import matplotlib.cm as cm
import numpy as np

import matplotlib.pyplot as plt
from scipy import ndimage
from scipy.ndimage import measurements
from scipy.ndimage.filters import gaussian_filter
from scipy.ndimage.interpolation import affine_transform, map_coordinates
from scipy.ndimage.morphology import (distance_transform_cdt,
                                      distance_transform_edt)
from skimage import morphology as morph

from tensorpack.dataflow.imgaug import ImageAugmentor
from tensorpack.utils.utils import get_rng

from misc.utils import cropping_center, bounding_box

####
class GenInstance(ImageAugmentor):
    def __init__(self, crop_shape=None):
        super(GenInstance, self).__init__()
        self.crop_shape = crop_shape
    
    def reset_state(self):
        self.rng = get_rng(self)

    def _fix_mirror_padding(self, ann):
        """
        Deal with duplicated instances due to mirroring in interpolation
        during shape augmentation (scale, rotation etc.)
        """
        current_max_id = np.amax(ann)
        inst_list = list(np.unique(ann))
        try: # remove background
           inst_list.remove(0) # 0 is background
        except ValueError:
            pass
        for inst_id in inst_list:
            inst_map = np.array(ann == inst_id, np.uint8)
            remapped_ids = measurements.label(inst_map)[0]
            remapped_ids[remapped_ids > 1] += current_max_id
            ann[remapped_ids > 1] = remapped_ids[remapped_ids > 1]
            current_max_id = np.amax(ann)
        return ann

####
class GenInstanceContourMap(GenInstance):
    # TODO: put the doc on for this
    """
    Input annotation must be of original shape.
    """
    def __init__(self, mode, crop_shape=None):
        super(GenInstanceContourMap, self).__init__()
        self.crop_shape = crop_shape
        self.mode = mode

    def _augment(self, img, _):
        img = np.copy(img)
        orig_ann = img[...,0] # instance ID map
        fixed_ann = self._fix_mirror_padding(orig_ann)
        fixed_ann = orig_ann
        # re-cropping with fixed instance id map
        #crop_ann = cropping_center(fixed_ann, self.crop_shape)

        # setting 1 boundary pix of each instance to background
        inner_map = np.zeros(fixed_ann.shape[:2], np.uint8)
        contour_map = np.zeros(fixed_ann.shape[:2], np.uint8)

        inst_list = list(np.unique(fixed_ann))

        try: # remove background
           inst_list.remove(0) # 0 is background
        except ValueError:
            pass
        
        if self.mode == 'seg_gland':
            k_disk = np.array([
                [0, 0, 0, 0, 1, 0, 0, 0, 0],
                [0, 0, 0, 1, 1, 1, 0, 0, 0],
                [0, 0, 1, 1, 1, 1, 1, 0, 0],
                [0, 1, 1, 1, 1, 1, 1, 1, 0],
                [1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 1, 1, 1, 1, 1, 1, 1, 0],
                [0, 0, 1, 1, 1, 1, 1, 0, 0],
                [0, 0, 0, 1, 1, 1, 0, 0, 0],
                [0, 0, 0, 0, 1, 0, 0, 0, 0],
            ], np.uint8)
        else:
            k_disk = np.array([
                [0, 0, 1, 0, 0],
                [0, 1, 1, 1, 0],
                [1, 1, 1, 1, 1],
                [0, 1, 1, 1, 0],
                [0, 0, 1, 0, 0],
            ], np.uint8)

        for inst_id in inst_list:
            inst_map = np.array(fixed_ann == inst_id, np.uint8)
            inner = cv2.erode(inst_map, k_disk, iterations=1)
            outer = cv2.dilate(inst_map, k_disk, iterations=1)
            inner_map += inner
            contour_map += outer - inner
        inner_map[inner_map > 0] = 1 # binarize
        contour_map[contour_map > 0] = 1 # binarize
        bg_map = 1 - (inner_map + contour_map)
        img = np.dstack([inner_map, contour_map, bg_map, img[...,1:]])
        return img
####

class GenInstanceMarkerMap(GenInstance):
    """
    Input annotation must be of original shape.
    Perform following operation:
        1) Remove the 1px of boundary of each instance
           to create separation between touching instances
        2) Generate the weight map from the result of 1)
           according to the unet paper equation.
    Args:
        wc (dict)        : Dictionary of weight classes.
        w0 (int/float)   : Border weight parameter.
        sigma (int/float): Border width parameter.
    """
    def __init__(self, wc=None, w0=10.0, sigma=4.0, crop_shape=None):
        super(GenInstanceMarkerMap, self).__init__()
        self.crop_shape = crop_shape
        self.wc = wc
        self.w0 = w0
        self.sigma = sigma

    def _erode_obj(self, ann):
        new_ann = np.zeros(ann.shape[:2], np.int32)
        inst_list = list(np.unique(ann))
        inst_list.remove(0) # 0 is background

        inner_map = np.zeros(ann.shape[:2], np.uint8)
        contour_map = np.zeros(ann.shape[:2], np.uint8)

        k = np.array([
                [0, 0, 0, 1, 0, 0, 0],
                [0, 0, 1, 1, 1, 0, 0],
                [0, 1, 1, 1, 1, 1, 0],
                [1, 1, 1, 1, 1, 1, 1],
                [0, 1, 1, 1, 1, 1, 0],
                [0, 0, 1, 1, 1, 0, 0],
                [0, 0, 0, 1, 0, 0, 0],
            ], np.uint8)

        for inst_id in inst_list:
            inst_map = np.array(ann == inst_id, np.uint8)
            inner = cv2.erode(inst_map, k, iterations=1)
            outer = cv2.dilate(inst_map, k, iterations=1)
            inner_map += inner
            contour_map += outer - inner
        inner_map[inner_map > 0] = 1 # binarize
        contour_map[contour_map > 0] = 1 # binarize
        bg_map = 1 - (inner_map + contour_map)

        return inner_map, contour_map, bg_map

    def _get_weight_map(self, ann, inst_list):
        if len(inst_list) <= 1: # 1 instance only
            return np.zeros(ann.shape[:2])
        stacked_inst_bgd_dst = np.zeros(ann.shape[:2] +(len(inst_list),))

        for idx, inst_id in enumerate(inst_list):
            inst_bgd_map = np.array(ann != inst_id , np.uint8)
            inst_bgd_dst = distance_transform_edt(inst_bgd_map)
            stacked_inst_bgd_dst[...,idx] = inst_bgd_dst

        near1_dst = np.amin(stacked_inst_bgd_dst, axis=2)
        near2_dst = np.expand_dims(near1_dst ,axis=2)
        near2_dst = stacked_inst_bgd_dst - near2_dst
        near2_dst[near2_dst == 0] = np.PINF # very large
        near2_dst = np.amin(near2_dst, axis=2)
        near2_dst[ann > 0] = 0 # the instances
        near2_dst = near2_dst + near1_dst
        # to fix pixel where near1 == near2
        near2_eve = np.expand_dims(near1_dst ,axis=2)
        # to avoide the warning of a / 0
        near2_eve = (1.0 + stacked_inst_bgd_dst) / (1.0 + near2_eve)
        near2_eve[near2_eve != 1] = 0
        near2_eve = np.sum(near2_eve, axis=2)
        near2_dst[near2_eve > 1] = near1_dst[near2_eve > 1]
        #
        pix_dst = near1_dst + near2_dst
        pen_map = pix_dst / self.sigma
        pen_map = self.w0 * np.exp(- pen_map**2 / 2)
        pen_map[ann > 0] = 0 # inner instances zero
        return pen_map

    def _augment(self, img, _):
        img = np.copy(img)
        orig_ann = img[...,0] # instance ID map
        orig_ann_copy = orig_ann.copy()
        fixed_ann = self._fix_mirror_padding(orig_ann)
        # setting 1 boundary pix of each instance to background
        inner_map, contour_map, bg_map = self._erode_obj(fixed_ann)

        # cant do the shortcut because near2 also needs instances 
        # outside of cropped portion
        inst_list = list(np.unique(fixed_ann))
        inst_list.remove(0) # 0 is background
        wmap = self._get_weight_map(fixed_ann, inst_list)

        if self.wc is None:             
            wmap += 1 # uniform weight for all classes
        else:
            class_weights = np.zeros_like(fixed_ann.shape[:2])
            for class_id, class_w in self.wc.items():
                class_weights[fixed_ann == class_id] = class_w
            wmap += class_weights

        # fix other maps to align
        img[fixed_ann == 0] = 0 
        orig_ann[orig_ann>0] = 1
        img = np.dstack([orig_ann_copy, inner_map, contour_map, bg_map, wmap])

        return img

####


class GaussianBlur(ImageAugmentor):
    """ Gaussian blur the image with random window size"""
    def __init__(self, max_size=3):
        """
        Args:
            max_size (int): max possible Gaussian window size would be 2 * max_size + 1
        """
        super(GaussianBlur, self).__init__()
        self.max_size = max_size

    def _get_augment_params(self, img):
        sx, sy = self.rng.randint(1, self.max_size, size=(2,))
        sx = sx * 2 + 1
        sy = sy * 2 + 1
        return sx, sy

    def _augment(self, img, s):
        return np.reshape(cv2.GaussianBlur(img, s, sigmaX=0, sigmaY=0,
                                           borderType=cv2.BORDER_REPLICATE), img.shape)

####

class GenInstanceHV(GenInstance):   
    """
        Input annotation must be of original shape.
        
        The map is calculated only for instances within the crop portion
        but based on the original shape in original image.
    
        Perform following operation:
        Obtain the horizontal and vertical distance maps for each
        nuclear instance.
    """

    def _augment(self, img, _):
        img = np.copy(img)
        orig_ann = img[...,0] # instance ID map
        fixed_ann = self._fix_mirror_padding(orig_ann)
        # re-cropping with fixed instance id map
        crop_ann = cropping_center(fixed_ann, self.crop_shape)
        # TODO: deal with 1 label warning
        crop_ann = morph.remove_small_objects(crop_ann, min_size=30)

        x_map = np.zeros(orig_ann.shape[:2], dtype=np.float32)
        y_map = np.zeros(orig_ann.shape[:2], dtype=np.float32)

        inst_list = list(np.unique(crop_ann))
        inst_list.remove(0) # 0 is background
        for inst_id in inst_list:
            inst_map = np.array(fixed_ann == inst_id, np.uint8)
            inst_box = bounding_box(inst_map)

            # expand the box by 2px
            # Because we first pad the ann at line 207, the bboxes
            # will remain valid after expansion
            inst_box[0] -= 2
            inst_box[2] -= 2
            inst_box[1] += 2
            inst_box[3] += 2

            inst_map = inst_map[inst_box[0]:inst_box[1],
                                inst_box[2]:inst_box[3]]

            if inst_map.shape[0] < 2 or \
                inst_map.shape[1] < 2:
                continue

            # instance center of mass, rounded to nearest pixel
            inst_com = list(measurements.center_of_mass(inst_map))
            
            inst_com[0] = int(inst_com[0] + 0.5)
            inst_com[1] = int(inst_com[1] + 0.5)

            inst_x_range = np.arange(1, inst_map.shape[1]+1)
            inst_y_range = np.arange(1, inst_map.shape[0]+1)
            # shifting center of pixels grid to instance center of mass
            inst_x_range -= inst_com[1]
            inst_y_range -= inst_com[0]
            
            inst_x, inst_y = np.meshgrid(inst_x_range, inst_y_range)

            # remove coord outside of instance
            inst_x[inst_map == 0] = 0
            inst_y[inst_map == 0] = 0
            inst_x = inst_x.astype('float32')
            inst_y = inst_y.astype('float32')

            # normalize min into -1 scale
            if np.min(inst_x) < 0:
                inst_x[inst_x < 0] /= (-np.amin(inst_x[inst_x < 0]))
            if np.min(inst_y) < 0:
                inst_y[inst_y < 0] /= (-np.amin(inst_y[inst_y < 0]))
            # normalize max into +1 scale
            if np.max(inst_x) > 0:
                inst_x[inst_x > 0] /= (np.amax(inst_x[inst_x > 0]))
            if np.max(inst_y) > 0:
                inst_y[inst_y > 0] /= (np.amax(inst_y[inst_y > 0]))

            ####
            x_map_box = x_map[inst_box[0]:inst_box[1],
                              inst_box[2]:inst_box[3]]
            x_map_box[inst_map > 0] = inst_x[inst_map > 0]

            y_map_box = y_map[inst_box[0]:inst_box[1],
                              inst_box[2]:inst_box[3]]
            y_map_box[inst_map > 0] = inst_y[inst_map > 0]

        img = img.astype('float32')
        img = np.dstack([img, x_map, y_map])

        return img
####

class BinarizeLabel(ImageAugmentor):
    """ Convert labels to binary maps"""
    def __init__(self):
        super(BinarizeLabel, self).__init__()

    def _get_augment_params(self, img):
        return None

    def _augment(self, img, s):
        img = np.copy(img)
        arr = img[...,0]
        arr[arr > 0] = 1
        return img

####
class MedianBlur(ImageAugmentor):
    """ Median blur the image with random window size"""
    def __init__(self, max_size=3):
        """
        Args:
            max_size (int): max possible window size 
                            would be 2 * max_size + 1
        """
        super(MedianBlur, self).__init__()
        self.max_size = max_size

    def _get_augment_params(self, img):
        s = self.rng.randint(1, self.max_size)
        s = s * 2 + 1
        return s

    def _augment(self, img, ksize):
        return cv2.medianBlur(img, ksize)

####
class ElasticDeform(ImageAugmentor):
    def __init__(self, alpha=900, sigma=[8.5,11], border='constant',borderValue=0):
        """
        sigma: range of sigma value
        border: 'reflect', 'constant', 'nearest', 'mirror', 'wrap'
        borderValue: used when border mode is 'constant'
        """
        super(ElasticDeform, self).__init__()
        self._init(locals())
        pass
    def reset_state(self):
        self.rng = get_rng(self)
    def test_run(self, img):
        return self._augment(img, None)
    def _augment(self, img, _):
        """Elastic deformation of images as described in [Simard2003]_.
        .. [Simard2003] Simard, Steinkraus and Platt, "Best Practices for
        Convolutional Neural Networks applied to Visual Document Analysis", in
        Proc. of the International Conference on Document Analysis and
        Recognition, 2003.
        """

        shape = img.shape
        sigma_x = self.rng.uniform(low=self.sigma[0], high=self.sigma[1])
        sigma_y = self.rng.uniform(low=self.sigma[0], high=self.sigma[1])

        dx = gaussian_filter((self.rng.rand(*shape) * 2 - 1), sigma_x, 
                                    mode=self.border, cval=self.borderValue) * self.alpha
        dy = gaussian_filter((self.rng.rand(*shape) * 2 - 1), sigma_y, 
                                    mode=self.border, cval=self.borderValue) * self.alpha

        if len(shape) == 3:
            dz = np.zeros_like(dx)
            x, y, z = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), np.arange(shape[2]))
            indices = np.reshape(y+dy, (-1, 1)), np.reshape(x+dx, (-1, 1)), np.reshape(z, (-1, 1))
        
        else:
            x, y = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]))
            indices = np.reshape(y+dy, (-1, 1)), np.reshape(x+dx, (-1, 1))

        distored_img = map_coordinates(img, indices, order=1, mode='nearest')
        return distored_img.reshape(img.shape)