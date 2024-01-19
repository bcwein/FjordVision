import numpy as np
import os
import torch

from ultralytics import YOLO
from ultralytics.nn.modules.head import Detect
from ultralytics.utils import ops

import matplotlib.pyplot as plt
import matplotlib.patches as patches
import cv2
from PIL import Image

import json

class SaveIO:
    """Simple PyTorch hook to save the output of a nn.module."""
    def __init__(self):
        self.input = None
        self.output = None
        
    def __call__(self, module, module_in, module_out):
        self.input = module_in
        self.output = module_out

def load_and_prepare_model(model_path):
    # we are going to register a PyTorch hook on the important parts of the YOLO model,
    # then reverse engineer the outputs to get boxes and logits
    # first, we have to register the hooks to the model *before running inference*
    # then, when inference is run, the hooks will save the inputs/outputs of their respective modules
    model = YOLO(model_path)
    detect = None
    cv2_hooks = None
    cv3_hooks = None
    detect_hook = SaveIO()
    for i, module in enumerate(model.model.modules()):
        if isinstance(module, Detect):
            module.register_forward_hook(detect_hook)
            detect = module

            cv2_hooks = [SaveIO() for _ in range(module.nl)]
            cv3_hooks = [SaveIO() for _ in range(module.nl)]
            for i in range(module.nl):
                module.cv2[i].register_forward_hook(cv2_hooks[i])
                module.cv3[i].register_forward_hook(cv3_hooks[i])
            break
    input_hook = SaveIO()
    model.model.register_forward_hook(input_hook)

    # save and return these for later
    hooks = [input_hook, detect, detect_hook, cv2_hooks, cv3_hooks]

    return model, hooks

def calculate_iou(box1, box2):
    """
    Calculates the Intersection over Union (IoU) between two bounding boxes.

    Args:
        box1 (list): Bounding box coordinates [x1, y1, w1, h1].
        box2 (list): Bounding box coordinates [x2, y2, w2, h2].

    Returns:
        float: Intersection over Union (IoU) value.
    """
    x1, y1, w1, h1 = box1
    x2, y2, w2, h2 = box2

    intersect_x1 = max(x1, x2)
    intersect_y1 = max(y1, y2)
    intersect_x2 = min(x1 + w1, x2 + w2)
    intersect_y2 = min(y1 + h1, y2 + h2)

    intersect_area = max(0, intersect_x2 - intersect_x1 + 1) * max(0, intersect_y2 - intersect_y1 + 1)
    box1_area = w1 * h1
    box2_area = w2 * h2

    iou = intersect_area / float(box1_area + box2_area - intersect_area)
    return iou


# Apply Non-Maximum Suppression
def nms(boxes, iou_threshold=0.7):
    """
    Applies Non-Maximum Suppression (NMS) to a list of bounding box dictionaries.

    Args:
        boxes (list): List of dictionaries, each containing 'bbox', and 'activations'.
        iou_threshold (float, optional): Intersection over Union (IoU) threshold for NMS. Default is 0.7.

    Returns:
        list: List of selected bounding box dictionaries after NMS.
    """
    # Sort boxes by confidence score in descending order
    sorted_boxes = sorted(boxes, key=lambda x: max(x['activations']), reverse=True)

    # Keep the box with highest confidence and remove overlapping boxes
    delete_idxs = []
    for i, box0 in enumerate(sorted_boxes):
        for j, box1 in enumerate(sorted_boxes):
            if i < j and calculate_iou(box0['bbox'], box1['bbox']) > iou_threshold:
                delete_idxs.append(j)

    # Reverse the order of delete_idxs
    delete_idxs.reverse()

    # now delete by popping them in reverse order
    filtered_boxes = [box for idx, box in enumerate(sorted_boxes) if idx not in delete_idxs]

    return filtered_boxes


def results_predict(img_path, model, hooks, threshold=0.5, iou=0.7):
    """
    Run prediction with a YOLO model and apply Non-Maximum Suppression (NMS) to the results.

    Args:
        img_path (str): Path to an image file.
        model (YOLO): YOLO model object.
        hooks (list): List of hooks for the model.
        threshold (float, optional): Confidence threshold for detection. Default is 0.5.
        iou (float, optional): Intersection over Union (IoU) threshold for NMS. Default is 0.7.
        save_image (bool, optional): Whether to save the image with boxes plotted. Default is False.

    Returns:
        list: List of selected bounding box dictionaries after NMS.
    """
    # unpack hooks from load_and_prepare_model()
    input_hook, detect, detect_hook, cv2_hooks, cv3_hooks = hooks

    # run inference; we don't actually need to store the results because
    # the hooks store everything we need
    model(img_path)

    # now reverse engineer the outputs to find the logits
    # see Detect.forward(): https://github.com/ultralytics/ultralytics/blob/b638c4ed9a24270a6875cdd47d9eeda99204ef5a/ultralytics/nn/modules/head.py#L22
    shape = detect_hook.input[0][0].shape  # BCHW
    x = []
    for i in range(detect.nl):
        x.append(torch.cat((cv2_hooks[i].output, cv3_hooks[i].output), 1))
    x_cat = torch.cat([xi.view(shape[0], detect.no, -1) for xi in x], 2)
    box, cls = x_cat.split((detect.reg_max * 4, detect.nc), 1)

    # assumes batch size = 1 (i.e. you are just running with one image)
    # if you want to run with many images, throw this in a loop
    batch_idx = 0
    xywh_sigmoid = detect_hook.output[0][batch_idx]

    # figure out the original img shape and model img shape so we can transform the boxes
    img_shape = input_hook.input[0].shape[2:]
    orig_img_shape = model.predictor.batch[1][batch_idx].shape[:2]

    # compute predictions
    boxes = []
    for i in range(xywh_sigmoid.shape[-1]): # for each predicted box...
        x0, y0, x1, y1, *class_probs_after_sigmoid = xywh_sigmoid[:,i]
        x0, y0, x1, y1 = ops.scale_boxes(img_shape, np.array([x0.cpu(), y0.cpu(), x1.cpu(), y1.cpu()]), orig_img_shape)
        
        boxes.append({
            'image_id': img_path,
            'bbox': [x0.item(), y0.item(), x1.item(), y1.item()], # xyxy
            'bbox_xcycwh': [(x0.item() + x1.item())/2, (y0.item() + y1.item())/2, x1.item() - x0.item(), y1.item() - y0.item()],
            'activations': [p.item() for p in class_probs_after_sigmoid]
        })

    # NMS
    # we can keep the activations and logits around via the YOLOv8 NMS method, but only if we
    # append them as an additional time to the prediction vector. It's a weird hacky way to do it, but
    # it works. We also have to pass in the num classes (nc) parameter to make it work.
    boxes_for_nms = torch.stack([
        torch.tensor([*b['bbox_xcycwh'], *b['activations'], *b['activations']]) for b in boxes
    ], dim=1).unsqueeze(0)
    
    # do the NMS
    nms_results = ops.non_max_suppression(boxes_for_nms, conf_thres=threshold, iou_thres=iou, nc=detect.nc)[0]
    
    # unpack it and return it
    boxes = []
    for b in range(nms_results.shape[0]):
        box = nms_results[b, :]
        x0, y0, x1, y1, conf, cls, *acts_and_logits = box
        activations = acts_and_logits[:detect.nc]
        box_dict = {
            'bbox': [x0.item(), y0.item(), x1.item(), y1.item()], # xyxy
            'bbox_xywh': [(x0.item() + x1.item())/2, (y0.item() + y1.item())/2, x1.item() - x0.item(), y1.item() - y0.item()],
            'best_conf': conf.item(),
            'best_cls': cls.item(),
            'image_id': img_path,
            'activations': [p.item() for p in activations],
        }
        boxes.append(box_dict)

    return boxes


def run_predict(input_path, model, hooks, score_threshold=0.5, iou_threshold=0.7):
    """
    Run prediction with a YOLO model.

    Args:
        input_path (str): Path to an image file or txt file containing paths to image files.
        model (YOLO): YOLO model object.
        hooks (list): List of hooks for the model.
        threshold (float, optional): Confidence threshold for detection. Default is 0.5.
        iou_threshold (float, optional): Intersection over Union (IoU) threshold for NMS. Default is 0.7.
        save_image (bool, optional): Whether to save the image with boxes plotted. Default is False.
        save_json (bool, optional): Whether to save the results in a json file. Default is False.

    Returns:
        list: List of selected bounding box dictionaries for all the images given as input.
    """

    img_paths = [input_path]

    all_results = []

    for img_path in img_paths:
        results = results_predict(img_path, model, hooks, score_threshold, iou=iou_threshold)

        all_results.extend(results)

    return all_results