import numpy as np
from lienp.SE3 import SE3
from lienp.SO3 import SO3
import os, cv2, json, yaml

import rclpy
from rclpy.node import Node
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import Image, CompressedImage
from geometry_msgs.msg import PoseStamped, TransformStamped
from visualization_msgs.msg import Marker
from tf2_ros import TransformException
from tf2_ros.transform_listener import TransformListener
from tf2_ros.buffer import Buffer

def get_3dbox(box_dim):
    vtx = (-1,1)
    box = []
    for ix in vtx:
        for iy in vtx:
            for iz in vtx:

                box.append(np.array([ix, iy, iz]) * box_dim/2)
    box = np.array(box)
    return box

class MocapCam(Node):
    def __init__(self, cam_name, info_dir=None, target_name=None):
        super().__init__("MoCap_camera")
        self.bridge = CvBridge()
        self.cam_name = cam_name
        self.target_name = target_name
        self.target_size = np.array([0.3, 0.3, 0.2])
        self.target_3dbox = get_3dbox(self.target_size)

        with open(f'{info_dir}/{cam_name}.yaml', 'r') as fs:
            cam_info = yaml.safe_load(fs)
        if cam_info is not None:
            self.K = np.array(cam_info["camera_matrix"]["data"])
            self.cam_dist = np.array(cam_info["distortion_coefficients"]["data"])
            self.K_opt, dist_valid_roi = cv2.getOptimalNewCameraMatrix(self.K.reshape(3,3), self.cam_dist, (1600,1200), 1, (1600, 1200))
            R_model2cam = np.array([
                [0, 1, 0],
                [0, 0, 1],
                [1, 0, 0],        
            ])

            # SE2, transform image coordiantes to match the book
            SE2_pix2image = np.array([
                [-1, 0,  1600],
                [0, -1, 1200],
                [0, 0,  1],
            ])
            K_ = SE2_pix2image @ self.K_opt @ R_model2cam

            self.K_t = np.block([
                [K_, np.zeros((3,1))],
                ])
        
        with open(f"{info_dir}/{cam_name}_mocap_calib.json") as fs:
            calib_info = json.load(fs)
            self.R_rel = np.array(calib_info['R'])
            self.c_rel = np.array(calib_info['c'])
            self.q_rel = SO3.to_quat(self.R_rel)
            self.T_rel = SE3(R=self.R_rel, c=self.c_rel)

        self.delay = rclpy.time.Duration(seconds=0.1)

        self.T_mocap = SE3(np.zeros(6))
        self.T_cam = None

        self.tf_buffer_ = Buffer()
        self.tf_listener_ = TransformListener(self.tf_buffer_, self)

        self.cam_mocap_pose_sub_ = self.create_subscription(
            PoseStamped,
            f"{cam_name}/pose",
            self.cam_mocap_pose_cb,
            10,
        )

        if target_name is not None:
            self.target_pose = PoseStamped()
            self.target_markers = Marker()
            self.target_mocap_pose_sub_ = self.create_subscription(
                PoseStamped,
                f"{target_name}/pose",
                self.target_mocap_pose_cb,
                10
            )

            self.target_marker_sub_ = self.create_subscription(
                Marker,
                f"{target_name}/markers",
                self.target_marker_cb,
                10
            )

        self.img_sub_ = self.create_subscription(
            Image,
            f"{cam_name}/image_raw",
            self.image_cb,
            10,
        )

        self.cam_pose_pub_ = self.create_publisher(
            PoseStamped,
            f"{cam_name}/pose/calibrated",
            10
        )

        self.img_pub_ = self.create_publisher(
            CompressedImage,
            f"{cam_name}/image_rect/projection",
            10
        )
    
    def cam_mocap_pose_cb(self, msg: PoseStamped):
        pos = msg.pose.position
        ori = msg.pose.orientation
        c = np.array([pos.x, pos.y, pos.z])
        q = np.array([ori.w, ori.x, ori.y, ori.z])
        R = SO3.from_quat(q).T
        self.T_mocap = SE3(R=R, c=c)
        self.T_cam = self.T_rel @ self.T_mocap

        try:
            stamp = msg.header.stamp
            lookup_time_ = rclpy.time.Time.from_msg(stamp) - self.delay
            # t = self.tf_buffer_.lookup_transform(
            #     'map', 
            #     f'{self.cam_name}/calibrated',
            #     lookup_time_)
            # c = np.array([t.transform.translation.x, t.transform.translation.y, t.transform.translation.z])
            # q = np.array([t.transform.rotation.w, t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z])
            # R = SO3.from_quat(q).T
            # self.T_cam = SE3(R=R, c=c)

        except ValueError as e:
            self.get_logger().info(f"{e}")

        except TransformException as ex:
            self.get_logger().info(
                        f'Could not transform map to {self.cam_name}/calibrated: {ex}')
            return

    def target_mocap_pose_cb(self, msg: PoseStamped):
        self.target_pose = msg
        # self.get_logger().info(f"fuck: {self.target_pose.pose.position}")
        # pos = msg.pose.position
        # ori = msg.pose.orientation
        # c = np.array([pos.x, pos.y, pos.z])
        # q = np.array([ori.w, ori.x, ori.y, ori.z])
        # R = SO3.from_quat(q).T
        # self.T_target = SE3(R=R, c=c)

    def target_marker_cb(self, msg: Marker):
        self.target_markers = msg

    def image_cb(self, msg):
        img = self.bridge.imgmsg_to_cv2(msg, 'bgr8')
        img_rect = cv2.undistort(img, self.K.reshape(3,3), self.cam_dist, None, newCameraMatrix=self.K_opt)
        ox = self.target_pose.pose.position.x
        oy = self.target_pose.pose.position.y
        oz = self.target_pose.pose.position.z
        obj_h = np.array([ox, oy, oz, 1])
        oqw = self.target_pose.pose.orientation.w
        oqx = self.target_pose.pose.orientation.x
        oqy = self.target_pose.pose.orientation.y
        oqz = self.target_pose.pose.orientation.z
        T_obj = SE3(R=SO3.from_quat(np.array([oqw,oqx,oqy,oqz])).T, c=np.array([ox,oy,oz]))
        
        markers_h = T_obj.inv @ np.hstack([self.target_3dbox, np.ones((8,1))]).T
        # markers = np.array([[p.x, p.y, p.z] for p in self.target_markers.points])
        # markers_h = np.hstack([markers, np.ones((markers.shape[0],1))])
        if self.T_cam is not None:
            pix_h = self.K_t @ self.T_cam.M @ obj_h
            pix = pix_h[0:-1]/pix_h[-1]
            img_rect = cv2.circle(img_rect, pix.astype(int), 50, (255,0,0), 2)

            pix_h = self.K_t @ self.T_cam.M @ markers_h
            pix = (pix_h[0:2,:] / pix_h[-1,:]).T
            for p in pix:
                img_rect = cv2.circle(img_rect, p.astype(int), 5, (0,255,0), 1)
            # self.get_logger().info(f"pix: {pix}\n")

            # img_msg = self.bridge.cv2_to_imgmsg(img_rect, encoding='bgr8')
            img_msg = self.bridge.cv2_to_compressed_imgmsg(img_rect)
            img_msg.header.frame_id = msg.header.frame_id
            img_msg.header.stamp = self.get_clock().now().to_msg()                  
            self.img_pub_.publish(img_msg)

            # cv2.imshow("camera view", img_rect)
            # keyboard = cv2.waitKey(1)
            # if keyboard == ord('q') or keyboard == 27:
            #     exit(0)

def main(args=None):
    rclpy.init()
    cam_name = 'camera0'
    info_dir = os.path.abspath( os.path.join(os.path.dirname(''), os.pardir)) + "/camera_info/"   
    
    node = MocapCam(cam_name=cam_name, info_dir=info_dir, target_name='drone1')
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # node.onshutdown()
        node.destroy_node()
        rclpy.try_shutdown()

if __name__ == '__main__':
    main()