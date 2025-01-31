#!/usr/bin/env python3

# Copyright 1993-2019 NVIDIA Corporation.  All rights reserved.
#
# NOTICE TO LICENSEE:
#
# This source code and/or documentation ("Licensed Deliverables") are
# subject to NVIDIA intellectual property rights under U.S. and
# international Copyright laws.
#
# These Licensed Deliverables contained herein is PROPRIETARY and
# CONFIDENTIAL to NVIDIA and is being provided under the terms and
# conditions of a form of NVIDIA software license agreement by and
# between NVIDIA and Licensee ("License Agreement") or electronically
# accepted by Licensee.  Notwithstanding any terms or conditions to
# the contrary in the License Agreement, reproduction or disclosure
# of the Licensed Deliverables to any third party without the express
# written consent of NVIDIA is prohibited.
#
# NOTWITHSTANDING ANY TERMS OR CONDITIONS TO THE CONTRARY IN THE
# LICENSE AGREEMENT, NVIDIA MAKES NO REPRESENTATION ABOUT THE
# SUITABILITY OF THESE LICENSED DELIVERABLES FOR ANY PURPOSE.  IT IS
# PROVIDED "AS IS" WITHOUT EXPRESS OR IMPLIED WARRANTY OF ANY KIND.
# NVIDIA DISCLAIMS ALL WARRANTIES WITH REGARD TO THESE LICENSED
# DELIVERABLES, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY,
# NONINFRINGEMENT, AND FITNESS FOR A PARTICULAR PURPOSE.
# NOTWITHSTANDING ANY TERMS OR CONDITIONS TO THE CONTRARY IN THE
# LICENSE AGREEMENT, IN NO EVENT SHALL NVIDIA BE LIABLE FOR ANY
# SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, OR ANY
# DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
# WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
# ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE
# OF THESE LICENSED DELIVERABLES.
#
# U.S. Government End Users.  These Licensed Deliverables are a
# "commercial item" as that term is defined at 48 C.F.R. 2.101 (OCT
# 1995), consisting of "commercial computer software" and "commercial
# computer software documentation" as such terms are used in 48
# C.F.R. 12.212 (SEPT 1995) and is provided to the U.S. Government
# only as a commercial end item.  Consistent with 48 C.F.R.12.212 and
# 48 C.F.R. 227.7202-1 through 227.7202-4 (JUNE 1995), all
# U.S. Government End Users acquire the Licensed Deliverables with
# only those rights set forth herein.
#
# Any use of the Licensed Deliverables in individual and commercial
# software must include, in the user documentation and internal
# comments to the code, the above Disclaimer and U.S. Government End
# Users Notice.
#

import sys, os
import time
import numpy as np
import cv2
import tensorrt as trt
from PIL import Image,ImageDraw
import rospy

from std_msgs.msg import String
from yolov3_trt_ros.msg import BoundingBox, BoundingBoxes

# from cv_bridge import CvBridge
from sensor_msgs.msg import Image as Imageros

from data_processing import PreprocessYOLO, PostprocessYOLO, ALL_CATEGORIES
import common
import calibration_parser

TRT_LOGGER = trt.Logger(trt.Logger.WARNING)

CFG = "/home/nvidia/xycar_ws/src/yolov3_trt_ros/src/yolov3-tiny_tstl_416.cfg"
TRT = '/home/nvidia/xycar_ws/src/yolov3_trt_ros/src/model_epoch3950.trt'
NUM_CLASS = 9
#INPUT_IMG = '/home/nvidia/xycar_ws/src/yolov3_trt_ros/src/video1_2.png'
CALIBRATION = '/home/nvidia/xycar_ws/src/xycar_device/usb_cam/calibration/ost.yaml'
GRID = "/home/nvidia/xycar_ws/src/yolov3_trt_ros/grid_image_arrow.jpg"

# bridge = CvBridge()
xycar_image = np.empty(shape=[0])

class yolov3_trt(object):
    def __init__(self):
        self.cfg_file_path = CFG
        self.num_class = NUM_CLASS
        width, height, masks, anchors = parse_cfg_wh(self.cfg_file_path)
        self.engine_file_path = TRT
        self.show_img = True

        _, _, self.homography = calibration_parser.read_yaml_file(CALIBRATION)

        # Two-dimensional tuple with the target network's (spatial) input resolution in HW ordered
        input_resolution_yolov3_WH = (width, height)
        # Create a pre-processor object by specifying the required input resolution for YOLOv3
        self.preprocessor = PreprocessYOLO(input_resolution_yolov3_WH)

        # Output shapes expected by the post-processor
        output_channels = (self.num_class + 5) * 3
        if len(masks) == 2:
            self.output_shapes = [(1, output_channels, height//32, width//32), (1, output_channels, height//16, width//16)]
        else:
            self.output_shapes = [(1, output_channels, height//32, width//32), (1, output_channels, height//16, width//16), (1, output_channels, height//8, width//8)]

        postprocessor_args = {"yolo_masks": masks,                    # A list of 3 three-dimensional tuples for the YOLO masks
                              "yolo_anchors": anchors,
                              "obj_threshold": 0.5,                                               # Threshold for object coverage, float value between 0 and 1
                              "nms_threshold": 0.3,                                               # Threshold for non-max suppression algorithm, float value between 0 and 1
                              "yolo_input_resolution": input_resolution_yolov3_WH,
                              "num_class": self.num_class}

        self.postprocessor = PostprocessYOLO(**postprocessor_args)

        self.engine = get_engine(self.engine_file_path)

        self.context = self.engine.create_execution_context()

        self.detection_pub = rospy.Publisher('/yolov3_trt_ros/detections', BoundingBoxes, queue_size=1)

    def detect(self):
        rate = rospy.Rate(10)
        image_sub = rospy.Subscriber("/usb_cam/image_raw", Imageros, img_callback)
        while not rospy.is_shutdown():
            rate.sleep()

            # Do inference with TensorRT

            inputs, outputs, bindings, stream = common.allocate_buffers(self.engine)

            # if xycar_image is empty, skip inference
            if xycar_image.shape[0] == 0:
                continue

            # if self.show_img:
            #     img = cv2.cvtColor(xycar_image, cv2.COLOR_BGR2RGB)
            #     # img = img[80:540, : ]
            #     # BEV = cv2.warpPerspective(img, self.homography, (640, 480), flags=cv2.INTER_LINEAR)
            #     cv2.imshow("img", img)
            #     cv2.waitKey(1)

            image = self.preprocessor.process(xycar_image)
            # Store the shape of the original input image in WH format, we will need it for later
            shape_orig_WH = (image.shape[3], image.shape[2])
            # Set host input to the image. The common.do_inference function will copy the input to the GPU before executing.
            start_time = time.time()
            inputs[0].host = image
            trt_outputs = common.do_inference(self.context, bindings=bindings, inputs=inputs, outputs=outputs, stream=stream)

            # Before doing post-processing, we need to reshape the outputs as the common.do_inference will give us flat arrays.
            trt_outputs = [output.reshape(shape) for output, shape in zip(trt_outputs, self.output_shapes)]

            # Run the post-processing algorithms on the TensorRT outputs and get the bounding box details of detected objects
            boxes, classes, scores = self.postprocessor.process(trt_outputs, shape_orig_WH)

            latency = time.time() - start_time
            fps = 1 / latency

            # find points on grid
            grid_image = cv2.imread(GRID, cv2.IMREAD_COLOR)
            grid_image, grid_points = return_boxpoint(grid_image, boxes, classes, ALL_CATEGORIES)
            out_grid.write(grid_image)

            # Draw the bounding boxes onto the original input image and save it as a PNG file
            # print(boxes, classes, scores)
            if self.show_img:
                img_show = np.array(np.transpose(image[0], (1,2,0)) * 255, dtype=np.uint8)
                obj_detected_img = draw_bboxes(Image.fromarray(img_show), boxes, scores, classes, ALL_CATEGORIES)
                obj_detected_img_np = np.array(obj_detected_img)
                show_img = cv2.cvtColor(obj_detected_img_np, cv2.COLOR_RGB2BGR)
                cv2.putText(show_img, "FPS:" + str(int(fps)), (10,50),cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,0), 2, 1)
                # cv2.imshow("result", show_img)
                # cv2.imshow("grid", grid_image)
                out_img.write(show_img)
                cv2.waitKey(1)

            #publish detected objects boxes and classes
            self.publisher(boxes, scores, classes, grid_points)

    def _write_message(self, detection_results, boxes, scores, classes, grid_points):
        """ populate output message with input header and bounding boxes information """
        if boxes is None:
            return None
        for box, score, category, grid_point in zip(boxes, scores, classes, grid_points):
            # Populate darknet message
            minx, miny, width, height = box
            detection_msg = BoundingBox()
            detection_msg.xmin = int(minx)
            detection_msg.xmax = int(minx + width)
            detection_msg.ymin = int(miny)
            detection_msg.ymax = int(miny + height)
            detection_msg.probability = score
            detection_msg.id = int(category)
            point_x, point_y = grid_point
            detection_msg.x = int(point_x)
            detection_msg.y = int(point_y)
            detection_results.bounding_boxes.append(detection_msg)
        return detection_results

    def publisher(self, boxes, confs, classes, grid_points):
        """ Publishes to detector_msgs
        Parameters:
        boxes (List(List(int))) : Bounding boxes of all objects
        confs (List(double))	: Probability scores of all objects
        classes  (List(int))	: Class ID of all classes
        """
        detection_results = BoundingBoxes()
        self._write_message(detection_results, boxes, confs, classes, grid_points)
        self.detection_pub.publish(detection_results)


#parse width, height, masks and anchors from cfg file
def parse_cfg_wh(cfg):
    masks = []
    with open(cfg, 'r') as f:
        lines = f.readlines()
        for line in lines:
            if 'width' in line:
                w = int(line[line.find('=')+1:].replace('\n',''))
            elif 'height' in line:
                h = int(line[line.find('=')+1:].replace('\n',''))
            elif 'anchors' in line:
                anchor = line.split('=')[1].replace('\n','')
                anc = [int(a) for a in anchor.split(',')]
                anchors = [(anc[i*2], anc[i*2+1]) for i in range(len(anc) // 2)]
            elif 'mask' in line:
                mask = line.split('=')[1].replace('\n','')
                m = tuple(int(a) for a in mask.split(','))
                masks.append(m)
    return w, h, masks, anchors

def img_callback(data):
    global xycar_image
    # xycar_image = bridge.imgmsg_to_cv2(data, "bgr8")
    xycar_image = np.frombuffer(data.data, dtype = np.uint8).reshape(data.height, data.width, -1)

def draw_bboxes(image_raw, bboxes, confidences, categories, all_categories, bbox_color='blue'):
    """Draw the bounding boxes on the original input image and return it.

    Keyword arguments:
    image_raw -- a raw PIL Image
    bboxes -- NumPy array containing the bounding box coordinates of N objects, with shape (N,4).
    categories -- NumPy array containing the corresponding category for each object,
    with shape (N,)
    confidences -- NumPy array containing the corresponding confidence for each object,
    with shape (N,)
    all_categories -- a list of all categories in the correct ordered (required for looking up
    the category name)
    bbox_color -- an optional string specifying the color of the bounding boxes (default: 'blue')
    """
    draw = ImageDraw.Draw(image_raw)
    if bboxes is None and confidences is None and categories is None:
        return image_raw
    for box, score, category in zip(bboxes, confidences, categories):
        x_coord, y_coord, width, height = box
        left = max(0, np.floor(x_coord + 0.5).astype(int))
        top = max(0, np.floor(y_coord + 0.5).astype(int))
        right = min(image_raw.width, np.floor(x_coord + width + 0.5).astype(int))
        bottom = min(image_raw.height, np.floor(y_coord + height + 0.5).astype(int))

        draw.rectangle(((left, top), (right, bottom)), outline=bbox_color)
        draw.text((left, top - 12), '{0} {1:.2f}'.format(all_categories[category], score), fill=bbox_color)

    return image_raw

def return_boxpoint(grid_image, bboxes, categories, all_categories):
    _, _, homography = calibration_parser.read_yaml_file(CALIBRATION)
    grid_points = []

    if bboxes is None:
        return grid_image, grid_points
    for box, category in zip(bboxes, categories):
        x_coord, y_coord, width, height = box
        left = max(0, np.floor(x_coord + 0.5).astype(int))
        right = min(416, np.floor(x_coord + width + 0.5).astype(int))
        mid = (left + right) / 2
        bottom = min(416, np.floor(y_coord + height + 0.5).astype(int))
        image_point = np.array([mid, bottom, 1])
        # print("image_point : ", image_point)

        estimation_distance = np.dot(homography, image_point)

        x = estimation_distance[0]
        y = estimation_distance[1]
        z = estimation_distance[2]

        point = (int(x // z) * 2, int(y // z) * 2)
        grid_point = (int(x // z) * 2 + 270, 540 - int(y // z) * 2)
        grid_points.append(point)
        cv2.circle(grid_image, (grid_point), 10, (0, 255, 0), -1)
        cv2.putText(grid_image, '{}'.format(all_categories[category]), (grid_point[0], grid_point[1] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

    return grid_image, grid_points

def get_engine(engine_file_path=""):
    """Attempts to load a serialized engine if available, otherwise builds a new TensorRT engine and saves it."""
    if os.path.exists(engine_file_path):
        # If a serialized engine exists, use it instead of building an engine.
        # print("Reading engine from file {}".format(engine_file_path))
        with open(engine_file_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
            return runtime.deserialize_cuda_engine(f.read())
    else:
        print("no trt model")
        sys.exit(1)

if __name__ == '__main__':
    yolo = yolov3_trt()
    rospy.init_node('yolov3_trt_ros', anonymous=True)

    fourcc = cv2.VideoWriter_fourcc(*'DIVX')
    out_img = cv2.VideoWriter('/home/nvidia/xycar_ws/src/yolov3_trt_ros/output/detection.avi', fourcc, 15, (416,416))
    out_grid = cv2.VideoWriter('/home/nvidia/xycar_ws/src/yolov3_trt_ros/output/bev.avi', fourcc, 15, (540, 540))
    yolo.detect()
    out_img.release()
    out_grid.release()

