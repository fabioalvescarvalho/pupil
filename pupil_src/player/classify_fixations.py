'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2014  Pupil Labs

 Distributed under the terms of the CC BY-NC-SA License.
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import cv2
from plugin import Plugin
import numpy as np
import atb
from glfw import *
from gl_utils import adjust_gl_view, draw_gl_texture, clear_gl_screen, draw_gl_point_norm, draw_gl_point, draw_gl_polyline, draw_gl_polyline_norm, basic_gl_setup
from ctypes import c_float, c_bool, c_int
from methods import denormalize,normalize
from player_methods import transparent_circle
import logging
from scan_path import Scan_Path

logger = logging.getLogger(__name__)

class Classify_Fixations(Plugin):
    """docstring
    This plugin classifies fixations and saccades by measuring dispersion and duration of gaze points 

    Methods of fixation detection are based on prior literature
        (Salvucci & Goldberg, ETRA, 2000) http://www.cs.drexel.edu/~salvucci/publications/Salvucci-ETRA00.pdf
        (Munn et al., APGV, 2008) http://www.cis.rit.edu/vpl/3DPOR/website_files/Munn_Stefano_Pelz_APGV08.pdf
        (Evans et al, JEMR, 2012) http://www.jemr.org/online/5/2/6

    Smooth Pursuit/Ego-motion accounted for by optical flow in Scan Path plugin: 
        Reference literature (Kinsman et al. "Ego-motion compensation improves fixation detection in wearable eye tracking," ACM 2011)
    
    Fixations general knowledge from literature review
        + Goldberg et al. - fixations rarely < 100ms and range between 200ms and 400ms in duration (Irwin, 1992 - fixations dependent on task between 150ms - 600ms)
        + Very short fixations are considered not meaningful for studying behavior - eye+brain require time for info to be registered (see Munn et al. APGV, 2008)
        + Fixations are rarely longer than 800ms in duration
            + Smooth Pursuit is exception and different motif
            + If we do not set a maximum duration, we will also detect smooth pursuit (which is acceptable since we compensate for VOR)
    Terms
        + dispersion (spatial) = how much spatial movement is allowed within one fixation (in visual angular degrees or pixels)
        + duration (temporal) = what is the minimum time required for gaze data to be within dispersion threshold?
        + cohesion (spatial+temporal) = is the cluster of 

    Overview Diagram
        + Scan path supplies a window into the past set by user and supplies 'recent_pupil_positions' list (variable duration set by user)
        + The anchor point 'p' is located at the temporal center of recent_pupil_positions

        past[       recent_pupil_positions      ]current
            [-----------------p-----------------] 


    """
    def __init__(self, g_pool=None,distance=8.0,samp_width=300,show_saccades=False,gui_settings={'pos':(10,470),'size':(300,100),'iconified':False}):
        super(Classify_Fixations, self).__init__()

        self.g_pool = g_pool
        # let the plugin work after most other plugins
        self.order = .7

        # user settings
        self.distance = c_float(float(distance))
        self.samp_width = c_int(int(samp_width))
        self.show_saccades = c_bool(bool(show_saccades))
        self.min_duration = 0.10
        self.max_duration = 0.40
        self.gui_settings = gui_settings

        self.sp_active = True

        # algorithm working data
        self.raw_dispersion_history = {}
        self.vis_dispersion_history = {}
        self.timestamp_history = []
        # degrees per pixel with c930e = 0.061282654954069871
        # 16.3 pixels = 1 degree 
        # 8 pixels = approx 0.5 degrees vis angle

        self.sample_pt = None
        self.past_pt = None
        self.present_pt = None
        self.candidates = []


        #debug window
        self.suggested_size = 640,480
        self._window = None
        self.window_should_open = False
        self.window_should_close = False

    def update(self,frame,recent_pupil_positions,events):
        img = frame.img
        img_shape = img.shape[:-1][::-1] # width,height
        self.norm_distance = self.distance.value/img_shape[1]

        # init debug window
        if self.window_should_open:
            self.open_window((frame.img.shape[1],frame.img.shape[0]))
        if self.window_should_close:
            self.close_window()

        if self._window:
            debug_img = np.zeros(frame.img.shape,frame.img.dtype)

        # initialize Scan Path so we can use its history and optical flow
        if any(isinstance(p,Scan_Path) for p in self.g_pool.plugins):
            if self.sp_active:
                pass
            else:
                self.set_bar_ok(True)
                self.sp_active = True
        else:
            if self.sp_active:
                self.set_bar_ok(False)
                self.sp_active = False
            else:
                pass
        
        # find distance between each gaze point pairwise
        for gp1, gp2 in zip(recent_pupil_positions[:-1], recent_pupil_positions[1:]):
            dispersion = self.manhattan_dist(gp1['norm_gaze'] ,gp2['norm_gaze'])
            
            if gp1['timestamp'] not in self.timestamp_history:
                self.raw_dispersion_history[gp1['timestamp']] = dispersion
                self.vis_dispersion_history[gp1['timestamp']] = dispersion + 0.5
                self.timestamp_history.append(gp1['timestamp']) 

        # visualizations in debug window
        if self._window:
            pts = zip( list(np.arange(0.,1.,1.0/self.samp_width.value)), map(self.vis_dispersion_history.get, self.timestamp_history[-self.samp_width.value:-1]) )
            self.gl_display_in_window(pts)

        

        # for p in recent_pupil_positions:
        #     p['dispersion'] = self.plugin_history.get(p['timestamp'],)

        recent_pupil_positions.sort(key=lambda k: k['timestamp']) #this may be redundant...
        # logger.debug("dict: %s" %(self.d))


        # # current hack for drawing fixations and saccades without vis_circle
        # pts = [p for p in recent_pupil_positions if p.has_key('type') and p['type'] is 'fixation']
        # pts = [denormalize(pt['norm_gaze'],frame.img.shape[:-1][::-1],flip_y=True) for pt in pts if pt['norm_gaze'] is not None]
        # for pt in pts:
        #     transparent_circle(frame.img, pt, radius=20, color=(0,40,255,200), thickness=2)

        # if self.show_saccades.value:
        #     pts = [p for p in recent_pupil_positions if p.has_key('type') and p['type'] is 'saccade']
        #     pts = [denormalize(pt['norm_gaze'],frame.img.shape[:-1][::-1],flip_y=True) for pt in pts if pt['norm_gaze'] is not None]
        #     for pt in pts:
        #         transparent_circle(frame.img, pt, radius=5, color=(255,150,0,200), thickness=-1)
                

    def init_gui(self,pos=None):
        import atb
        pos = self.gui_settings['pos']
        atb_label = "Filter Fixations"
        self._bar = atb.Bar(name =self.__class__.__name__+str(id(self)), label=atb_label,
            help="polyline", color=(50, 50, 50), alpha=50,
            text='light', position=pos,refresh=.1, size=self.gui_settings['size'])

        self._bar.iconified = self.gui_settings['iconified']
        self._bar.add_var('dispersion in pixels',self.distance,min=0,step=0.1)
        self._bar.add_var('debug sample width',self.samp_width,min=10,step=1)
        self._bar.add_var('show saccades',self.show_saccades)
        self._bar.add_button("open debug window", self.toggle_window,help="Visualization of gaze velocity/time.")
        self._bar.add_button('remove',self.unset_alive)
   
    def toggle_window(self):
        if self._window:
            self.window_should_close = True
        else:
            self.window_should_open = True


    def open_window(self,size):
        if not self._window:
            if 0: #we are not fullscreening
                monitor = self.monitor_handles[self.monitor_idx.value]
                mode = glfwGetVideoMode(monitor)
                height,width= mode[0],mode[1]
            else:
                monitor = None
                height,width= size

            active_window = glfwGetCurrentContext()
            self._window = glfwCreateWindow(height, width, "Plugin Window", monitor=monitor, share=None)
            if not 0:
                glfwSetWindowPos(self._window,200,0)

            self.on_resize(self._window,height,width)

            #Register callbacks
            glfwSetWindowSizeCallback(self._window,self.on_resize)
            # glfwSetKeyCallback(self._window,self.on_key)
            glfwSetWindowCloseCallback(self._window,self.on_close)

            # gl_state settings
            glfwMakeContextCurrent(self._window)
            basic_gl_setup()

            # refresh speed settings
            glfwSwapInterval(0)

            glfwMakeContextCurrent(active_window)

            self.window_should_open = False

    # window calbacks
    def on_resize(self,window,w, h):
        active_window = glfwGetCurrentContext()
        glfwMakeContextCurrent(window)
        adjust_gl_view(w,h)
        glfwMakeContextCurrent(active_window)

    def on_close(self,window):
        self.window_should_close = True

    def close_window(self):
        if self._window:
            glfwDestroyWindow(self._window)
            self._window = None
            self.window_should_close = False

    def gl_display_in_window(self,pts):
        active_window = glfwGetCurrentContext()
        glfwMakeContextCurrent(self._window)
        clear_gl_screen()
        # gl stuff that will show on your plugin window goes here
        # draw_gl_texture(img,interpolation=False)
        offset = 0.5
        draw_gl_polyline_norm(([0.0,offset],[1.0,offset]),(.0,.0,.0,0.5),type="Strip")
        draw_gl_polyline_norm(([0.0,self.norm_distance+offset],[1.0,self.norm_distance+offset]),(1.0,0.4,.0,0.5),type="Strip")

        draw_gl_polyline_norm(pts,(0.,1.,0,1.),type="Strip")
        glfwSwapBuffers(self._window)
        glfwMakeContextCurrent(active_window)

    def set_bar_ok(self,ok):
        if ok:
            self._bar.color = (50, 50, 50)
            self._bar.label = "Filter Fixations"
        else:
            self._bar.color = (250, 50, 50)
            self._bar.label = "Filter Fixations: Turn on Scan_Path!"

    def unset_alive(self):
        self.alive = False


    def get_init_dict(self):
        d = {'distance':self.distance.value, 'show_saccades':self.show_saccades.value}
        if hasattr(self,'_bar'):
            gui_settings = {'pos':self._bar.position,'size':self._bar.size,'iconified':self._bar.iconified}
            d['gui_settings'] = gui_settings
        return d


    def clone(self):
        return Filter_Fixations(**self.get_init_dict())


    def cleanup(self):
        """ called when the plugin gets terminated.
        This happends either voluntary or forced.
        if you have an atb bar or glfw window destroy it here.
        """
        if self._window:
            self.close_window()
        self._bar.destroy()

    def manhattan_dist_denormalize(self, gp1, gp2, img_shape):
        gp1_norm = denormalize(gp1['norm_gaze'], img_shape,flip_y=True)
        gp2_norm = denormalize(gp2['norm_gaze'], img_shape,flip_y=True)
        x_dist =  abs(gp1_norm[0] - gp2_norm[0])
        y_dist = abs(gp1_norm[1] - gp2_norm[1])
        man = x_dist + y_dist
        return man

    def manhattan_dist(self, gp1, gp2):
        x_dist =  abs(gp1[0] - gp2[0])
        y_dist = abs(gp1[1] - gp2[1])
        man = x_dist + y_dist
        return man

