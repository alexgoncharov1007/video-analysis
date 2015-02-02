'''
Created on Dec 19, 2014

@author: David Zwicker <dzwicker@seas.harvard.edu>
'''

from __future__ import division

import cPickle as pickle 
import collections
import itertools
import logging
import math
import os

import numpy as np
import cv2
from scipy import interpolate, spatial
from shapely import geometry

from data_structures.cache import cached_property
from video.io import VideoFile, ImageWindow
from video.composer import VideoComposer
from video.utils import display_progress
from video.filters import FilterMonochrome, FilterResize
from video.analysis import curves, image, regions
from video.analysis.active_contour import ActiveContour

from video import debug  # @UnusedImport

from .tail import Tail
from .kymograph import Kymograph
from .parameters import parameters_tracking, parameters_tracking_special
from segmentation_tracking.annotation import TackingAnnotations, SegmentPicker



class TailSegmentationTracking(object):
    """ class managing the tracking of mouse tails in videos
    
    The data structure in which we store the result is as follows:
    `self.results` is a dictionary containing the following entries:
        `tail_trajectories` which is a dictionary with as many entries as there
                            are tails in the video. It has entries
            `%tail_id%` which are again dictionaries containing a Tail object
                        for each frame of the video. The keys for this
                        dictionary are the frame_ids
        `kymographs` is a dictionary with as many entries as there are tails
            `%tail_id%` is a dictionary with two entries, one for each
                        kymograph for each side of the tail
    """
    
    def __init__(self, video_file, output_video=None, parameter_set='default',
                 parameters=None, show_video=False):
        """
        `video_file` is the input video
        `output_video` is the video file where the output is written to
        `show_video` indicates whether the video should be shown while processing
        """
        self.video_file = video_file
        self.output_video = output_video
        self.show_video = show_video

        self.name = os.path.splitext(os.path.split(video_file)[1])[0]
        self.annotations = TackingAnnotations(self.name)
        print self.annotations.database
        
        # load the parameters for the tracking
        self.params = parameters_tracking[parameter_set].copy()
        if self.name in parameters_tracking_special:
            logging.info('There are special parameters for this video.')
            logging.debug('The parameters are: %s',
                          parameters_tracking_special[self.name])
            self.params.update(parameters_tracking_special[self.name])
        if parameters is not None:
            self.params.update(parameters)

        # setup structure for saving data
        self.result = {'parameters': self.params.copy()}
        self.frame_id = None
        self.frame = None
        self._frame_cache = {}


    def load_video(self, make_video=False):
        """ loads and returns the video """
        # load video and make it grey scale
        self.video = VideoFile(self.video_file)
        self.video = FilterMonochrome(self.video)
        
        if self.params['input/zoom_factor'] != 1:
            self.video = FilterResize(self.video,
                                      self.params['input/zoom_factor'],
                                      even_dimensions=True)
        
        # restrict video to requested frames
        if self.params['input/frames']:
            frames = self.params['input/frames']
            self.video = self.video[frames[0]: frames[1]]
            
        
        # setup debug output 
        zoom_factor = self.params['output/zoom_factor']
        if make_video:
            if self.output_video is None:
                raise ValueError('Video output filename is not set.')
            self.output = VideoComposer(self.output_video, size=self.video.size,
                                        fps=self.video.fps, is_color=True,
                                        zoom_factor=zoom_factor)
            if self.show_video:
                self.debug_window = ImageWindow(self.output.shape,
                                                title=self.video_file,
                                                multiprocessing=False)
            else:
                self.debug_window = None
                
        else:
            self.output = None


    @cached_property
    def frame_start(self):
        """ return start frame of the video """
        if self.params['input/frames']:
            frame_id = self.params['input/frames'][0]
            if frame_id is None:
                return 0
            else:
                return frame_id
        else:
            return 0
        
        
    def load_first_frame(self):
        """ loads the first frame into self.frame """
        if self.frame_id != 0:
            self._frame_cache = {} #< delete cache per frame
            self.frame_id = self.frame_start
            self.frame = self.video[0]
        return self.frame

    
    def track_tails(self, make_video=False):
        """ processes the video and locates the tails """
        # initialize the video
        self.load_video(make_video)
        
        # process first frame to find objects
        tails = self.process_first_frame()
        
        # initialize the result structure
        tail_trajectories = collections.defaultdict(dict)
        self.result['tail_trajectories'] = tail_trajectories
        
        # iterate through all frames
        iterator = display_progress(self.video)
        for self.frame_id, self.frame in enumerate(iterator, self.frame_start):
            self._frame_cache = {} #< delete cache per frame
            if make_video:
                self.set_video_background(tails)

            # adapt the object outlines
            self.adapt_tail_contours(tails)
            
            # store tail data in the result
            for tail_id, tail in enumerate(tails):
                tail_trajectories[tail_id][self.frame_id] = tail.copy()
            
            # update the debug output
            if make_video:
                self.update_video_output(tails, show_measurement_line=False)
            
        # save the data and close the videos
        self.save_result()
        self.close()
        
    
    def set_video_background(self, tails):
        """ sets the background of the video """
        if self.params['output/background'] == 'original':
            self.output.set_frame(self.frame, copy=True)
            
        elif self.params['output/background'] == 'potential':
            image = self.contour_potential
            lo, hi = image.min(), image.max()
            image = 255*(image - lo)/(hi - lo)
            self.output.set_frame(image)
            
        elif self.params['output/background'] == 'gradient':
            image = self.get_gradient_strenght(self.frame)
            lo, hi = image.min(), image.max()
            image = 255*(image - lo)/(hi - lo)
            self.output.set_frame(image)
            
        elif self.params['output/background'] == 'gradient_thresholded':
            image = self.get_gradient_strenght(self.frame)
            image = self.threshold_gradient_strength(image)
            lo, hi = image.min(), image.max()
            image = 255*(image - lo)/(hi - lo)
            self.output.set_frame(image)
            
        elif self.params['output/background'] == 'features':
            image, num_features = self.get_features(tails,
                                                    use_annotations=False)
            if num_features > 0:
                self.output.set_frame(image*(128//num_features))
            else:
                self.output.set_frame(np.zeros_like(self.frame))
            
        else:
            self.output.set_frame(np.zeros_like(self.frame))

    
    def process_first_frame(self):
        """ process the first frame to localize the tails """
        self.load_first_frame() #< stores frame in self.frame
        # locate tails roughly
        tails = self.locate_tails_roughly()
        # refine tails
        self.adapt_tail_contours_initially(tails)
        return tails
        
                    
    #===========================================================================
    # CONTOUR FINDING
    #===========================================================================
    
    
    @cached_property(cache='_frame_cache')
    def frame_blurred(self):
        """ blurs the current frame """
        return cv2.GaussianBlur(self.frame.astype(np.double), (0, 0),
                                self.params['gradient/blur_radius'])
        
    
    def get_gradient_strenght(self, frame):
        """ calculates the gradient strength of the image in frame """
        # smooth the image to be able to find smoothed edges
        if frame is self.frame:
            # take advantage of possible caching
            frame_blurred = self.frame_blurred
        else:
            frame_blurred = cv2.GaussianBlur(frame.astype(np.double), (0, 0),
                                             self.params['gradient/blur_radius'])
        
        # scale frame_blurred to [0, 1]
        cv2.divide(frame_blurred, 256, dst=frame_blurred)

        # do Sobel filtering to find the frame_blurred edges
        grad_x = cv2.Sobel(frame_blurred, cv2.CV_64F, 1, 0, ksize=5)
        grad_y = cv2.Sobel(frame_blurred, cv2.CV_64F, 0, 1, ksize=5)

        # calculate the gradient strength
        gradient_mag = frame_blurred #< reuse memory
        np.hypot(grad_x, grad_y, out=gradient_mag)
        
        return gradient_mag
    
        
    def threshold_gradient_strength(self, gradient_mag):
        """ thresholds the gradient strength such that features are emphasized
        """
        lo, hi = gradient_mag.min(), gradient_mag.max()
        threshold = lo + self.params['gradient/threshold']*(hi - lo)
        bw = (gradient_mag > threshold).astype(np.uint8)
        
        for _ in xrange(2):
            bw = cv2.pyrDown(bw)

        # do morphological opening to remove noise
        w = 2#0
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (w, w))
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
    
        # do morphological closing to locate objects
        w = 2#0
        bw = cv2.copyMakeBorder(bw, w, w, w, w, cv2.BORDER_CONSTANT, 0)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*w + 1, 2*w + 1))
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
        bw = bw[w:-w, w:-w].copy()

        for _ in xrange(2):
            bw = cv2.pyrUp(bw)
        
        return bw
        

    def get_features(self, tails=None, use_annotations=False, ret_raw=False):
        """ calculates a feature mask based on the image statistics """
        # calculate image statistics
        ksize = self.params['detection/statistics_window']
        _, var = image.get_image_statistics(self.frame, kernel='circle',
                                            ksize=ksize)

        # threshold the variance to locate features
        threshold = self.params['detection/statistics_threshold']*np.median(var)
        bw = (var > threshold).astype(np.uint8)
        
        if ret_raw:
            return bw
        
        # add features from the previous frames if present
        if tails:
            for tail in tails:
                # fill the features with the interior of the former tail
                polys = tail.polygon.buffer(-self.params['detection/shape_max_speed'])
                if not isinstance(polys, geometry.MultiPolygon):
                    polys = [polys]
                for poly in polys: 
                    cv2.fillPoly(bw, [np.array(poly.exterior.coords, np.int)],
                                 color=1)
                
                # calculate the distance to other tails to bound the current one
                buffer_dist = self.params['detection/shape_max_speed']
                poly_outer = tail.polygon.buffer(buffer_dist)
                for tail_other in tails:
                    if tail is not tail_other:
                        dist = tail.polygon.distance(tail_other.polygon)
                        if dist < buffer_dist:
                            # shrink poly_outer
                            poly_other = tail_other.polygon.buffer(dist/2)
                            poly_outer = poly_outer.difference(poly_other)
                    
                # make sure that this tail is separated from all the others
                coords = np.array(poly_outer.exterior.coords, np.int)
                cv2.polylines(bw, [coords], isClosed=True, color=0, thickness=2)

#         debug.show_image(self.frame, bw, wait_for_key=False)
        
        if use_annotations:
            lines = self.annotations['segmentation_dividers']
            if lines:
                logging.debug('Found %d annotation lines for segmenting',
                              len(lines))
                for line in lines:
                    cv2.line(bw, tuple(line[0]), tuple(line[1]), 0, thickness=3)
            else:
                logging.debug('Found no annotations for segmenting')

        # remove features at the edge of the image
        border = self.params['detection/border_distance']
        image.set_image_border(bw, size=border, color=0)
        
        # remove very thin features
        cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((3, 3)), dst=bw)

        # find features in the binary image
        contours, _ = cv2.findContours(bw.astype(np.uint8), cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)

        # determine the rectangle where objects can lie in
        h, w = self.frame.shape
        rect = regions.Rectangle(x=0, y=0, width=w, height=h)
        rect.buffer(-2*border)
        rect = geometry.Polygon(rect.contour)

        bw[:] = 0
        num_features = 0  
        for contour in contours:
            if cv2.contourArea(contour) > self.params['detection/area_min']:
                # check whether the object touches the border
                feature = geometry.Polygon(np.squeeze(contour))
                if rect.exterior.intersects(feature):
                    # fill the hole in the feature
                    difference = rect.difference(feature)
                    
                    if isinstance(difference, geometry.Polygon):
                        difference = [difference] #< make sure we handle a list
                        
                    for diff in difference:
                        if diff.area < self.params['detection/area_max']:
                            feature = feature.union(diff)
                
                # reduce feature, since detection typically overshoots
                features = feature.buffer(-0.5*self.params['detection/statistics_window'])
                
                if not isinstance(features, geometry.MultiPolygon):
                    features = [features]
                
                for feature in features:
                    if feature.area > self.params['detection/area_min']:
                        #debug.show_shape(feature, background=self.frame)
                        
                        # extract the contour of the feature 
                        contour = regions.get_enclosing_outline(feature)
                        contour = np.array(contour.coords, np.int)
                        
                        # fill holes inside the objects
                        num_features += 1
                        cv2.fillPoly(bw, [contour], num_features)

#         debug.show_image(self.frame, var, bw, wait_for_key=False)

        return bw, num_features
        
        
    def locate_tails_roughly(self):
        """ locate tail objects using thresholding """
        # find features, using annotations in the first frame        
        use_annotations = (self.frame_id == self.frame_start)
        labels, _ = self.get_features(use_annotations=use_annotations)

        # find the contours of these features
        contours, _ = cv2.findContours(labels, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)

        # locate the tails using these contours
        tails = []
        for contour in contours:
            if cv2.contourArea(contour) > self.params['detection/area_min']:
                points = contour[:, 0, :]
                threshold = 0.002 * curves.curve_length(points)
                points = curves.simplify_curve(points, threshold)
                tails.append(Tail(points))
        
#         debug.show_shape(*[t.contour_ring for t in tails], background=self.frame,
#                          wait_for_key=False)
    
        logging.info('Found %d tail(s) in frame %d', len(tails), self.frame_id)
        return tails
        
        
    @cached_property(cache='_frame_cache')
    def contour_potential(self):
        """ calculates the contour potential """ 
        #features = self.get_features[0]
        #gradient_mag = self.get_gradient_strenght(features > 0)
        
        gradient_mag = self.get_gradient_strenght(self.frame)
        return gradient_mag
        
        
    def adapt_tail_contours_initially(self, tails):
        """ adapt tail contour to frame, assuming that they could be quite far
        away """
        
        # setup active contour algorithm
        ac = ActiveContour(blur_radius=self.params['contour/blur_radius_initial'],
                           closed_loop=True,
                           alpha=self.params['contour/line_tension'], 
                           beta=self.params['contour/bending_stiffness'],
                           gamma=self.params['contour/adaptation_rate'])
        ac.max_iterations = self.params['contour/max_iterations']
        ac.set_potential(self.contour_potential)
        
        # get rectangle describing the interior of the frame
        height, width = self.frame.shape
        region_center = regions.Rectangle(0, 0, width, height)
        region_center.buffer(-self.params['contour/border_anchor_distance'])

        # adapt all the tails
        for tail in tails:
            # find points not at the center
            anchor_idx = ~region_center.points_inside(tail.contour)
            tail.contour = ac.find_contour(tail.contour, anchor_idx, anchor_idx)

            logging.debug('Active contour took %d iterations.',
                          ac.info['iteration_count'])

#         debug.show_shape(*[t.contour for t in tails],
#                          background=self.contour_potential)


    def adapt_tail_contours(self, tails):
        """ adapt tail contour to frame, assuming that they are already close """
        # get the tails that we want to adapt
        if self.params['detection/every_frame']:
            tails_new = self.locate_tails_roughly()
        else:
            tails_new = tails[:] #< copy list
        # 
        assert len(tails_new) == len(tails)
        
        # setup active contour algorithm
        ac = ActiveContour(blur_radius=self.params['contour/blur_radius'],
                           closed_loop=True,
                           alpha=self.params['contour/line_tension'],
                           beta=self.params['contour/bending_stiffness'],
                           gamma=self.params['contour/adaptation_rate'])
        ac.max_iterations = self.params['contour/max_iterations']
        #potential_approx = self.threshold_gradient_strength(gradient_mag)
        ac.set_potential(self.contour_potential)

#         debug.show_shape(*[t.contour for t in tails],
#                          background=self.contour_potential)        
        
        # get rectangle describing the interior of the frame
        height, width = self.frame.shape
        region_center = regions.Rectangle(0, 0, width, height)
        region_center.buffer(-self.params['contour/border_anchor_distance'])
        
        for k, tail in enumerate(tails):
            # find the tail that is closest
            idx = np.argmin([curves.point_distance(tail.centroid, t.centroid)
                             for t in tails_new])
            
            # adapt this contour to the potential
            tail = tails_new.pop(idx)
            
            # determine the points close to the boundary that will be anchored
            # at their position, because there are no features to track at the
            # boundary 
            anchor_idx = ~region_center.points_inside(tail.contour)
            
            # disable anchoring for points at the posterior end of the tail
            ps = tail.contour[anchor_idx]
            dists = spatial.distance.cdist(ps, [tail.endpoints[0]])
            dist_threshold = self.params['contour/typical_width']
            anchor_idx[anchor_idx] = (dists.flat > dist_threshold)
            
            # use an active contour algorithm to adapt the contour points
            contour = ac.find_contour(tail.contour, anchor_idx, anchor_idx)
            logging.debug('Active contour took %d iterations.',
                          ac.info['iteration_count'])
            
            # update the old tail to keep the identity of sides
            tails[k].update_contour(contour)
            
#         debug.show_shape(*[t.contour for t in tails],
#                          background=self.contour_potential)        
    
    
    #===========================================================================
    # TRACKING ANNOTATIONS
    #===========================================================================
    
    
    def annotate(self):
        """ add annotations to the video to help the segmentation """
        # initialize the video
        self.load_video()

        # determine the features of the first frame
        self.load_first_frame()
        features = self.get_features(ret_raw=True)

        # load previous annotations
        lines = self.annotations['segmentation_dividers']
        
        # use the segmentation picker to alter these segments
        picker = SegmentPicker(self.frame, features, lines)
        result = picker.run()
        if result == 'ok':
            # save the result if desired
            self.annotations['segmentation_dividers'] = picker.segments
            
    
    #===========================================================================
    # LINE SCANNING
    #===========================================================================
    
    
    def do_linescans(self, make_video=True):
        """ do the line scans for all current_tails """
        # initialize the video
        self.load_video(make_video)
        self.load_result()
        
        # load the previously track tail data
        try:
            tail_trajectories = self.result['tail_trajectories']
        except KeyError:
            raise ValueError('Tails have to be tracked before line scans can '
                             'be done.')
        current_tails = None
            
        # iterate through all frames
        iterator = display_progress(self.video)
        for self.frame_id, self.frame in enumerate(iterator, self.frame_start):
            self._frame_cache = {} #< delete cache per frame
            if make_video:
                self.set_video_background(current_tails)

            # do the line scans in each object
            current_tails = [] 
            for tail_trajectory in tail_trajectories.itervalues():
                try:
                    tail = tail_trajectory[self.frame_id]
                except KeyError:
                    break
                linescans= self.measure_single_tail(self.frame, tail)
                tail.data['line_scans'] = linescans
                current_tails.append(tail) #< safe for video output
            
            # update the debug output
            if make_video:
                self.update_video_output(current_tails,
                                         show_measurement_line=True)
            
        # save the data and close the videos
        self.save_result()
        self.close()
    
    
    def get_measurement_lines(self, tail):
        """
        determines the measurement segments that are used for the line scan
        """
        f_c = self.params['measurement/line_offset']
        f_o = 1 - f_c

        centerline = tail.centerline
        result = []
        for side in tail.sides:
            # find the line between the centerline and the ventral line
            points = []
            for p_c in centerline:
                p_o = curves.get_projection_point(side, p_c) #< outer line
                points.append((f_c*p_c[0] + f_o*p_o[0],
                               f_c*p_c[1] + f_o*p_o[1]))
                
            # do spline fitting to smooth the line
            smoothing = self.params['measurement/spline_smoothing']*len(points)
            tck, _ = interpolate.splprep(np.transpose(points),
                                         k=2, s=smoothing)
            
            points = interpolate.splev(np.linspace(-0.5, .8, 100), tck)
            points = zip(*points) #< transpose list
    
            # restrict centerline to object
            mline = geometry.LineString(points).intersection(tail.polygon)
            
            # pick longest line if there are many due to strange geometries
            if isinstance(mline, geometry.MultiLineString):
                mline = mline[np.argmax([l.length for l in mline])]
                
            result.append(np.array(mline.coords))
            
        return result   
    
    
    def measure_single_tail(self, frame, tail):
        """ do line scans along the measurement segments of the tails """
        l = self.params['measurement/line_scan_width']
        w = 2 #< width of each individual line scan
        result = []
        for line in self.get_measurement_lines(tail):
            ps = curves.make_curve_equidistant(line, spacing=2*w)
            profile = []
            for pp, p, pn in itertools.izip(ps[:-2], ps[1:-1], ps[2:]):
                # slope
                dx, dy = pn - pp
                dlen = math.hypot(dx, dy)
                dx /= dlen; dy /= dlen
                
                # get end points of line scan
                p1 = (p[0] + l*dy, p[1] - l*dx)
                p2 = (p[0] - l*dy, p[1] + l*dx)
                
                lscan = image.line_scan(frame, p1, p2, width=w)
                profile.append(lscan.mean())
                
                self.output.add_points([p1, p2], 1, 'w')
                
            result.append(profile)
            
        return result
    
    
    #===========================================================================
    # KYMOGRAPHS
    #===========================================================================
    

    def calculate_kymographs(self, align=False):
        """ calculates the kymographs from the tracking result data """
        self.load_result()
        tail_trajectories = self.result['tail_trajectories']
        
        # iterate over all tails found in the movie
        kymographs = collections.defaultdict(dict)
        for tail_id, tail_trajectory in tail_trajectories.iteritems():
            line_scans = [tail.data['line_scans']
                          for tail in tail_trajectory.itervalues()]
            # transpose data
            line_scans = zip(*line_scans)
            
            # iterate over all line scans (ventral and dorsal)
            for side_id, data in enumerate(line_scans):
                kymograph = Kymograph(data)
                if align:
                    kymograph.align_features()
                side = tail.line_names[side_id]
                kymograph[tail_id][side] = kymograph

        self.result['kymographs'] = kymographs                
        self.save_result()


    def plot_kymographs(self, outfile=None):
        """ plots a kymograph of the line scan data """
        import matplotlib.pyplot as plt
        plt.figure(figsize=(10, 4))
        
        for tail_id, kymographs in self.result['kymograph'].iteritems():
            for side, kymograph in kymographs.iteritems():
                plt.subplot(1, 2, side + 1)
                # create image
                plt.imshow(kymograph.get_image(), aspect='auto',
                           interpolation='none')
                plt.gray()
                plt.xlabel('Distance from posterior [4 pixels]')
                plt.ylabel('Time [frames]')
                plt.title(['ventral', 'dorsal'][side])
        
            plt.suptitle(self.name + ' Tail %d' % tail_id)
            if outfile is None:
                plt.show()
            else:
                plt.savefig(outfile % tail_id)
    
    
    #===========================================================================
    # INPUT/OUTPUT
    #===========================================================================
    
    
    @property
    def outfile(self):
        """ return the file name under which the results will be saved """
        return self.video_file.replace('.avi', '.pkl')


    def load_result(self):
        """ load data from a result file """
        with open(self.outfile, 'r') as fp:
            self.result.update(pickle.load(fp))
        logging.debug('Read results from file `%s`', self.outfile)


    def save_result(self):
        """ saves results as a pickle file """
        with open(self.outfile, 'w') as fp:
            pickle.dump(self.result, fp)
        logging.debug('Wrote results to file `%s`', self.outfile)
                
    
    def update_video_output(self, tails, show_measurement_line=True):
        """ updates the video output to both the screen and the file """
        video = self.output
        # add information on all tails
        for tail_id, tail in enumerate(tails):
            # mark all the segments that are important in the video
            mark_points = (video.zoom_factor < 1)
            video.add_line(tail.contour, color='b', is_closed=True,
                           width=3, mark_points=mark_points)
            video.add_line(tail.ventral_side, color='g', is_closed=False,
                           width=4, mark_points=mark_points)
            video.add_line(tail.centerline, color='g',
                           is_closed=False, width=5, mark_points=mark_points)

            # mark the points that we identified
            p_P, p_A = tail.endpoints
            n = (p_P - p_A)
            n = 50 * n / np.hypot(n[0], n[1])
            video.add_circle(p_P, 10, 'g')
            video.add_text('P', p_P + n, color='g', anchor='center middle')
            video.add_circle(p_A, 10, 'b')
            video.add_text('A', p_A - n, color='b', anchor='center middle')
            
            video.add_text(str('tail %d' % tail_id), tail.centroid,
                           color='w', anchor='center middle')
            
            if show_measurement_line:
                for k, line in enumerate(self.get_measurement_lines(tail)):
                    video.add_line(line[:-3], color='r', is_closed=False,
                                   width=5, mark_points=mark_points)
                    video.add_text(tail.line_names[k], line[-1], color='r',
                                   anchor='center middle')
        
        # add general information
        video.add_text(str(self.frame_id), (20, 20), size=2, anchor='top')
        
        if self.debug_window:
            self.debug_window.show(video.frame)
    
    
    def close(self):
        """ close the output video and the debug video """
        if self.output:
            self.output.close()
            if self.debug_window:
                self.debug_window.close()
        