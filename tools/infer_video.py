# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
##############################################################################

"""Perform inference on a single image or all images with a certain extension
(e.g., .jpg) in a folder.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import defaultdict
import argparse
import cv2  # NOQA (Must import before importing caffe2 due to bug in cv2)
import glob
import logging
import os
import sys
import time
import numpy as np

from caffe2.python import workspace

from detectron.core.config import assert_and_infer_cfg
from detectron.core.config import cfg
from detectron.core.config import merge_cfg_from_file
from detectron.utils.io import cache_url
from detectron.utils.logging import setup_logging
from detectron.utils.timer import Timer
import detectron.core.test_engine as infer_engine
import detectron.datasets.dummy_datasets as dummy_datasets
import detectron.utils.c2 as c2_utils
import detectron.utils.vis as vis_utils
# from tools.video_capture import Cap
# from tools.prep_visualizor import form_IUV_mask

c2_utils.import_detectron_ops()

# OpenCL may be enabled by default in OpenCV3; disable it because it's not
# thread safe and causes unwanted GPU memory allocations.
cv2.ocl.setUseOpenCL(False)

def convert_from_cls_format(cls_boxes, cls_segms, cls_keyps):
    """Convert from the class boxes/segms/keyps format generated by the testing
    code.
    """
    box_list = [b for b in cls_boxes if len(b) > 0]
    if len(box_list) > 0:
        boxes = np.concatenate(box_list)
    else:
        boxes = None
    if cls_segms is not None:
        segms = [s for slist in cls_segms for s in slist]
    else:
        segms = None
    if cls_keyps is not None:
        keyps = [k for klist in cls_keyps for k in klist]
    else:
        keyps = None
    classes = []
    for j in range(len(cls_boxes)):
        classes += [j] * len(cls_boxes[j])
    return boxes, segms, keyps, classes


def form_IUV_mask(
        im, im_name, output_dir, boxes, segms=None, keypoints=None, body_uv=None, thresh=0.9,
        kp_thresh=2, dpi=200, box_alpha=0.0, dataset=None, show_class=False,
        ext='pdf'):
    """Visual debugging of detections."""
    # if not os.path.exists(output_dir):
    #     os.makedirs(output_dir)

    if isinstance(boxes, list):
        boxes, segms, keypoints, classes = convert_from_cls_format(
            boxes, segms, keypoints)

    if boxes is None or boxes.shape[0] == 0 or max(boxes[:, 4]) < thresh:
        return

    #   DensePose Visualization Starts!!
    ##  Get full IUV image out
    IUV_fields = body_uv[1]
    #
    All_Coords = np.zeros(im.shape)
    All_inds = np.zeros([im.shape[0], im.shape[1]])
    K = 26
    ##
    inds = np.argsort(boxes[:, 4])
    ##
    for i, ind in enumerate(inds):
        entry = boxes[ind, :]
        if entry[4] > 0.65:
            entry = entry[0:4].astype(int)
            ####
            output = IUV_fields[ind]
            ####
            All_Coords_Old = All_Coords[entry[1]: entry[1] + output.shape[1], entry[0]:entry[0] + output.shape[2], :]
            All_Coords_Old[All_Coords_Old == 0] = output.transpose([1, 2, 0])[All_Coords_Old == 0]
            All_Coords[entry[1]: entry[1] + output.shape[1], entry[0]:entry[0] + output.shape[2], :] = All_Coords_Old
            ###
            CurrentMask = (output[0, :, :] > 0).astype(np.float32)
            All_inds_old = All_inds[entry[1]: entry[1] + output.shape[1], entry[0]:entry[0] + output.shape[2]]
            All_inds_old[All_inds_old == 0] = CurrentMask[All_inds_old == 0] * i
            All_inds[entry[1]: entry[1] + output.shape[1], entry[0]:entry[0] + output.shape[2]] = All_inds_old
    #
    All_Coords[:, :, 1:3] = 255. * All_Coords[:, :, 1:3]
    All_Coords[All_Coords > 255] = 255.
    All_Coords = All_Coords.astype(np.uint8)
    return All_Coords

def save_video(images,path,fps=30):
    fourcc = cv2.VideoWriter_fourcc("M","P","4","V")
    height,width=images[0].shape[:2]
    out = cv2.VideoWriter(path, fourcc, fps, (width, height))
    for image in images:
        out.write(image)
    out.release()
    cv2.destroyAllWindows()

class Cap:
    def __init__(self, path, step_size=1):
        self.path = path
        self.step_size = step_size
        self.curr_frame_no = 0

    def __enter__(self):
        self.cap = cv2.VideoCapture(self.path)
        return self

    def read(self):
        success, frame = self.cap.read()
        if not success:
            return success, frame
        for _ in range(self.step_size):
            s, f = self.cap.read()
            if not s:
                break

        return success, frame

    def read_all(self):
        frames_list = []
        while True:
            success, frame = self.cap.read()
            if not success:
                return frames_list

            frames_list.append(frame)

            for _ in range(self.step_size-1):
                s, f = self.cap.read()
                if not s:
                    return frames_list

    def __exit__(self, a, b, c):
        self.cap.release()
        cv2.destroyAllWindows()



def parse_args():
    parser = argparse.ArgumentParser(description='End-to-end inference')
    parser.add_argument(
        '--cfg',
        dest='cfg',
        help='cfg model file (/path/to/model_config.yaml)',
        default=None,
        type=str
    )
    parser.add_argument(
        '--wts',
        dest='weights',
        help='weights model file (/path/to/model_weights.pkl)',
        default=None,
        type=str
    )
    parser.add_argument(
        '--output-dir',
        dest='output_dir',
        help='directory for visualization pdfs (default: /tmp/infer_simple)',
        default='/tmp/infer_simple',
        type=str
    )
    parser.add_argument(
        '--step-size',
        dest='step_size',
        help='step size for video',
        default=1,
        type=int
    )
    parser.add_argument(
        '--image-ext',
        dest='image_ext',
        help='image file name extension (default: jpg)',
        default='jpg',
        type=str
    )
    parser.add_argument(
        'im_or_folder', help='image or folder of images', default=None
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    return parser.parse_args()


def main(args):
    logger = logging.getLogger(__name__)
    merge_cfg_from_file(args.cfg)
    cfg.NUM_GPUS = 1
    args.weights = cache_url(args.weights, cfg.DOWNLOAD_CACHE)
    assert_and_infer_cfg(cache_urls=False)
    model = infer_engine.initialize_model_from_cfg(args.weights)
    dummy_coco_dataset = dummy_datasets.get_coco_dataset()
    IUVs_List=[]
    images_read=False
    is_dir=os.path.isdir(args.im_or_folder)



    result_dir = os.path.basename(args.im_or_folder).split(".")[0]
    result_dir=os.path.join(args.output_dir,result_dir)
    print ("Result directory is {} ".format(result_dir))
    if not os.path.exists(result_dir):
        os.makedirs(result_dir)

    if is_dir:
        im_list = glob.iglob(args.im_or_folder + '/*.' + args.image_ext)

    else:
        cap = Cap(args.im_or_folder, args.step_size)
        with cap as cap:
            im_list = cap.read_all()
        images_read = True

    for i in range(len(im_list)):
        im_name = im_list[i] if is_dir else args.im_or_folder

        # out_name = os.path.join(
        #     args.output_dir, '{}'.format(os.path.basename(im_name) + '.pdf')
        # )
        # logger.info('Processing {} -> {}'.format(im_name, out_name))
        im = im_list[i] if not is_dir else cv2.imread(im_name)
        timers = defaultdict(Timer)
        t = time.time()
        with c2_utils.NamedCudaScope(0):
            cls_boxes, cls_segms, cls_keyps, cls_bodys = infer_engine.im_detect_all(
                model, im, None, timers=timers
            )
        logger.info('Inference time: {:.3f}s'.format(time.time() - t))
        for k, v in timers.items():
            logger.info(' | {}: {:.3f}s'.format(k, v.average_time))
        if i == 0:
            logger.info(
                ' \ Note: inference on the first image will be slower than the '
                'rest (caches and auto-tuning need to warm up)'
            )

        IUVs=form_IUV_mask(
            im[:, :, ::-1],  # BGR -> RGB for visualization
            im_name,
            args.output_dir,
            cls_boxes,
            cls_segms,
            cls_keyps,
            cls_bodys,
            dataset=dummy_coco_dataset,
            box_alpha=0.3,
            show_class=True,
            thresh=0.7,
            kp_thresh=2
        )

        # result_name = os.path.basename(args.im_or_folder).split('.')[0] + '{}_IUV.jpg'.format(i)
        out_name =os.path.join(result_dir, '{}_IUV.png'.format(i))

        # out_name = os.path.join(
        #     args.output_dir, result_name)
        print ("saving image at {}".format(out_name))
        # IUVs_List.append(IUVs)

        cv2.imwrite(out_name, IUVs)

    #make a video of iuvs and store it

    # video =IUVs_List[0]
    #store in the directory
    # result_name=os.path.basename(args.im_or_folder).split('.')[0]+'_IUV.mp4'
    # out_name = os.path.join(
    #     args.output_dir, result_name)
    # print ("saving video at {} "?.format(out_name))
    # cv2.imwrite(out_name, video)
    # save_video(IUVs_List,out_name)


if __name__ == '__main__':
    workspace.GlobalInit(['caffe2', '--caffe2_log_level=0'])
    setup_logging(__name__)
    args = parse_args()
    main(args)
