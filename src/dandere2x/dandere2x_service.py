import logging
import sys
import threading
import time

import colorlog
from colorlog import ColoredFormatter

from dandere2x.dandere2x_service_context import Dandere2xServiceContext
from dandere2x.dandere2x_service_controller import Dandere2xController
from dandere2x.dandere2x_service_request import Dandere2xServiceRequest
from dandere2xlib.core.merge_new import Merge
from dandere2xlib.core.residual_new import Residual
from dandere2xlib.mindiskusage_new import MinDiskUsage
from dandere2xlib.status_new import Status
from dandere2xlib.utils.dandere2x_utils import show_exception_and_exit, file_exists, create_directories
from wrappers.dandere2x_cpp_new import Dandere2xCppWrapper
from wrappers.waifu2x_new.realsr_ncnn_vulkan import RealSRNCNNVulkan
from wrappers.waifu2x_new.waifu2x_caffe import Waifu2xCaffe
from wrappers.waifu2x_new.waifu2x_converter_cpp import Waifu2xConverterCpp
from wrappers.waifu2x_new.waifu2x_ncnn_vulkan import Waifu2xNCNNVulkan


class Dandere2xServiceThread(threading.Thread):

    def __init__(self, service_request: Dandere2xServiceRequest):
        super().__init__(name=service_request.name)
        # Administrative Stuff
        # import sys
        # sys.excepthook = show_exception_and_exit  # set a custom except hook to prevent window from closing.
        # threading.Thread.__init__(self, name="Dandere2x Thread")
        self.color_log_format = "%(log_color)s%(asctime)-8s%(reset)s %(log_color)s%(levelname)-8s%(reset)s %(log_color)s%(filename)-8s%(reset)s %(log_color)s%(funcName)-8s%(reset)s: %(log_color)s%(message)s"
        self.file_log_format = "%(asctime)s %(levelname)s %(filename)s %(funcName)s %(message)s"
        self.set_console_logger()
        self.fh = None  # File Handler for log

        # Class Specific
        self.context = Dandere2xServiceContext(service_request)
        self.controller = Dandere2xController()
        self.log = logging.getLogger()
        self.threads_active = False

        # Class Specific Future Declarations
        """ 
        These are re-set later, but due to lack of python member-variable declarations, they're initially set here so the IDE can 
        do autocomplete corrections / predictions. It's important they're correctly re-assigned when self.run() is called. 
        """
        self.min_disk_demon = MinDiskUsage(self.context, self.controller)
        self.status_thread = Status(self.context, self.controller)
        self.dandere2x_cpp_thread = Dandere2xCppWrapper(self.context, self.controller)
        self.waifu2x = Waifu2xNCNNVulkan(context=self.context, controller=self.controller)
        self.residual_thread = Residual(self.context, self.controller)
        self.merge_thread = Merge(context=self.context, controller=self.controller)

    def run(self):
        self.log.info("Thread Started")
        create_directories(self.context.service_request.workspace, self.context.directories)

        self.log.info("Dandere2x Threads Set.. going live with the following context file.")
        self.context.log_all_variables()

        self.__extract_frames()
        self.__upscale_first_frame()

        self.min_disk_demon.start()
        self.dandere2x_cpp_thread.start()
        self.merge_thread.start()
        self.residual_thread.start()
        self.waifu2x.start()
        self.status_thread.start()
        self.threads_active = True

    def kill(self):
        """
        Kill Dandere2x entirely. Everything started as a thread within the scope of dandere2x_service.py can be killed with
        controller.kill() except for d2x_cpp, since that runs as a subprocess.

        As an analogy, imagine `controller` is a fishline that is passed to all threads, and we can `pull the cord` on
        all the threads back at once, eliminating the need to chase after all N threads.
        """
        self.log.warning("Dandere2x Killed - Standby")
        self.dandere2x_cpp_thread.kill()
        self.controller.kill()

    def join(self, timeout=None):
        self.log.info("Joined called.")

        while not self.threads_active:
            time.sleep(1)

        self.min_disk_demon.join()
        self.dandere2x_cpp_thread.join()
        self.merge_thread.join()
        self.residual_thread.join()
        self.waifu2x.join()
        self.status_thread.join()
        self.log.info("Joined finished.")

    # todo, remove this dependency.
    def _get_waifu2x_class(self, name: str):
        """ Returns a waifu2x object depending on what the user selected. """

        if name == "caffe":
            return Waifu2xCaffe(self.context)

        elif name == "converter_cpp":
            return Waifu2xConverterCpp(self.context)

        elif name == "vulkan":
            return Waifu2xNCNNVulkan(self.context)

        elif name == "realsr_ncnn_vulkan":
            return RealSRNCNNVulkan(self.context)

        else:
            print("no valid waifu2x selected")
            sys.exit(1)

    def __extract_frames(self):
        """ Extract the initial frames needed for a dandere2x to run depending on session type. """

        # if self.context.start_frame != 1:
        #     self.log.info("This is a resume session, extracting frames to where you left off.")
        #     self.min_disk_demon.progressive_frame_extractor.extract_frames_to(self.context.start_frame)

        self.min_disk_demon.extract_initial_frames()

    def __upscale_first_frame(self):
        """ The first frame of any dandere2x session needs to be upscaled fully, and this is done as it's own
        process. Ensuring the first frame can get upscaled also provides a source of error checking for the user. """

        # measure the time to upscale a single frame for printing purposes
        one_frame_time = time.time()
        self.waifu2x.upscale_file(
            input_image=self.context.input_frames_dir + "frame" + str(
                1) + ".jpg",
            output_image=self.context.merged_dir + "merged_" + str(
                1) + ".png")

        if not file_exists(
                self.context.merged_dir + "merged_" + str(1) + ".jpg"):
            """ Ensure the first file was able to get upscaled. We literally cannot continue if it doesn't. """
            self.log.error("Could not upscale first file. Dandere2x CANNOT continue.")
            self.log.error("Have you tried making sure your waifu2x works?")

            raise Exception("Could not upscale first file.. check logs file to see what's wrong")

        self.log.info("Time to upscale a single frame: %s " % str(round(time.time() - one_frame_time, 2)))

    def set_file_logger(self, file: str):
        self.log.info("Writing log-file at %s" % file)
        formatter = logging.Formatter(self.file_log_format)
        self.fh = logging.FileHandler(file, "w", "utf-8")
        self.fh.setFormatter(formatter)
        self.log.addHandler(self.fh)
        self.log.info("Log-file set.")

    def set_console_logger(self):
        """
        Create the logging class to be format print statements the dandere2x way.

        The formatted output resembles the following (roughly):
            2020-08-01 16:03:39,455 INFO     dandere2x_service.py : Hewwooo
            2020-08-01 16:03:39,456 WARNING  dandere2x_service.py : jeeez fuck this warning
            2020-08-01 16:03:39,456 ERROR    dandere2x_service.py : oh fuck fuck fuck stop the program an error occurred
        """

        formatter = ColoredFormatter(
            self.color_log_format,
            datefmt=None,
            reset=True,
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_white',
            },
            secondary_log_colors={},
            style='%'
        )

        self.handler = colorlog.StreamHandler()
        self.handler.setFormatter(formatter)

        logger = colorlog.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)
        logging.info("Dandere2x Console Logger Set")
