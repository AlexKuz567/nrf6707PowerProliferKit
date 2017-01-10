from __future__ import print_function
import datetime
import time

try:
    import PySide
    import pynrfjprog
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui
    import numpy as np
    import struct
    from libs.label import EditableLabel
    import libs.rtt as rtt
    import sys
    import platform
    # Check for python version error
    if sys.version_info[0] != 2:
        raise ValueError('Version error:\n \
        Python version in use: %d.%d.%d\n \
        PPK needs version >= 2.7.11' % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))
except ImportError as ie:
    # Catched if any packages are missing
    missing = str(ie).split("named ")[1]
    print("Software needs %s installed\nPlease run pip install %s and restart\r\n" % (missing, missing))
    input("Press any key to exit...")
    exit()
except ValueError as e:
    print (str(e))
    input("Press any key to exit...")
    exit()



GLOBAL_OFFSET = 0.0e-6
str_uA = u'[\u03bcA]'
str_delta = u'\u0394'

SAMPLE_INTERVAL = 13.0e-6
ADC_REF = 0.6
ADC_GAIN = 4.0
ADC_MAX = 8192.0

MEAS_RANGE_NONE = 0
MEAS_RANGE_LO = 1
MEAS_RANGE_MID = 2
MEAS_RANGE_HI = 3
MEAS_RANGE_INVALID = 4

MEAS_RANGE_POS = 14
MEAS_RANGE_MSK = (3 << 14)

MEAS_ADC_POS = 0
MEAS_ADC_MSK = 0x3FFF


def rms_flat(a):
    """
    Return the root mean square of all the elements of *a*, flattened out.
    https://gist.github.com/endolith/1257010
    """
    return np.sqrt(np.mean(np.absolute(a)**2))


class RTT_COMMANDS():
    RTT_CMD_TRIGGER_SET         = 0x01  # following trigger of type int16
    RTT_CMD_AVG_NUM_SET         = 0x02  # Number of samples x16 to average over
    RTT_CMD_TRIG_WINDOW_SET     = 0x03  # following window of type unt16
    RTT_CMD_TRIG_INTERVAL_SET   = 0x04  #
    RTT_CMD_SINGLE_TRIG         = 0x05
    RTT_CMD_RUN                 = 0x06
    RTT_CMD_STOP                = 0x07
    RTT_CMD_RANGE_SET           = 0x08
    RTT_CMD_LCD_SET             = 0x09
    RTT_CMD_TRIG_STOP           = 0x0A
    RTT_CMD_CALIBRATE_OFFSET    = 0x0B
    RTT_CMD_DUT                 = 0x0C
    RTT_CMD_SETVDD              = 0x0D
    RTT_CMD_SETVREFLO           = 0x0E
    RTT_CMD_SETVREFHI           = 0x0F
    RTT_CMD_TOGGLE_EXT_TRIG     = 0x11
    RTT_CMD_SET_RES_USER        = 0x12


class PlotData():
    ''' Global variables for data plots goes here, accessed by PlotData.var, not instanced '''
    trigger = 2500
    MEAS_RES_HI = None
    MEAS_RES_MID = None
    MEAS_RES_LO = None

    sample_interval = SAMPLE_INTERVAL
    avg_interval   = sample_interval * 10  # num of samples averaged per packet
    avg_timewindow = 2  # avg_interval * 1024
    current_meas_range = 0
    trig_interval   = sample_interval
    trig_timewindow = trig_interval * (512 + 0)

    avg_bufsize  = int(avg_timewindow / avg_interval)
    trig_bufsize = int(trig_timewindow / trig_interval)

    avg_x = np.linspace(0.0, avg_timewindow, avg_bufsize)
    avg_y = np.zeros(avg_bufsize, dtype=np.float)
    trig_x = np.linspace(0.0, trig_timewindow, trig_bufsize)
    trig_y = np.zeros(trig_bufsize, dtype=np.float)

    trigger_high = trigger >> 8
    trigger_low = trigger & 0xFF

    vref_hi = 0
    vref_lo = 0
    vdd     = 0

    shown_avg_curve = []

''' These two classes are made for thread safe execution of showing/closing
    a message box to avoid using QPixmap on main GUI thread
'''

avg_timeout = 200

class ShowInfoWindow(QtCore.QThread):
    show_calib_signal = QtCore.Signal(str, str)

    def __init__(self, title="PPK", info="Calibrating..."):
        QtCore.QThread.__init__(self)
        self.title = title
        self.info = info

    def __del__(self):
        self.wait()

    def run(self):
        self.show_calib_signal.emit(self.title, self.info)


class CloseInfoWindow(QtCore.QThread):
    close_calib_signal = QtCore.Signal()

    def __init__(self):
        QtCore.QThread.__init__(self)

    def __del__(self):
        self.wait()

    def run(self):
        self.close_calib_signal.emit()


class SettingsWindow(QtCore.QObject):
    def __init__(self, plot_data, plot_window):
        QtCore.QObject.__init__(self)
        self.rtt = None
        self.rtt_handler = None
        self.curs_avg_enabled = True
        self.curs_trig_enabled = True
        self.external_trig_enabled = False
        self.plot_window = plot_window
        self.board_id = None
        self.m_vdd = 3000

        self.settings_widget = QtGui.QWidget()          # Settings widget

        self.calibrated_res_lo  = 0
        self.calibrated_res_mid = 0
        self.calibrated_res_hi  = 0

        ico = QtGui.QIcon('images\icon.ico')
        self.settings_widget.setWindowIcon(ico)

        self.settings_widget.move(50, 50)

        self.msgBox = QtGui.QMessageBox()
        self.msgBox.setWindowTitle("Information")
        self.msgBox.setIconPixmap(QtGui.QPixmap('images\icon.ico'))

        self.settings_widget.setWindowTitle('Settings - Power Profiler Kit')
        self.settings_widget.setFixedWidth(375)
        self.settings_layout = QtGui.QVBoxLayout()  # Settings layout
        self.settings_layout.addWidget(self.logo_label())
        self.settings_layout.addLayout(self.average_settings())
        self.settings_layout.addWidget(self.trigger_settings())
        # self.settings_layout.addWidget(self.range_settings())
        self.settings_layout.addWidget(self.cursor_settings())
        self.settings_layout.addWidget(self.edit_colors_button())
        self.settings_layout.addWidget(self.edit_bg_button())
        # self.settings_layout.addWidget(self.calibrate_offset_button())
        self.settings_layout.addWidget(self.statusbar())
        self.settings_layout.addLayout(self.vrefs())
        self.settings_layout.addWidget(self.calibration_resistors())

        self.settings_widget.setLayout(self.settings_layout)
        self.settings_widget.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.settings_widget.destroyed.connect(self.destroyedEvent)
        self.settings_widget.show()
        print("Power Profiler Kit started, initializing...")

    def write_new_res(self, r1, r2, r3):
        r1_list = []
        r2_list = []
        r3_list = []

        # Pack the floats
        bufr1 = struct.pack('f', r1)
        bufr2 = struct.pack('f', r2)
        bufr3 = struct.pack('f', r3)

        # PPK receives byte packages, put them in a list
        for b in bufr1:
            r1_list.append(b)
        for b in bufr2:
            r2_list.append(b)
        for b in bufr3:
            r3_list.append(b)

        # Write the floats to PPK
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SET_RES_USER,
                                ord(r1_list[0]), ord(r1_list[1]), ord(r1_list[2]), ord(r1_list[3]),
                                ord(r2_list[0]), ord(r2_list[1]), ord(r2_list[2]), ord(r2_list[3]),
                                ord(r3_list[0]), ord(r3_list[1]), ord(r3_list[2]), ord(r3_list[3])
                                ])

    def destroyedEvent(self):
        try:
            QtGui.QApplication.quit()
        except:
            pass
        QtGui.QApplication.quit()

    def logo_label(self):
        logo = QtGui.QPixmap('images\\NordicS_small.png')
        image_label = QtGui.QLabel()
        image_label.setPixmap(logo)
        return image_label

    def set_rtt_instance(self, rtt):
        self.rtt = rtt

    def average_settings(self):
        top_layout              = QtGui.QHBoxLayout()   # Container and dut switch in same layout
        dut_button_layout       = QtGui.QVBoxLayout()
        gb_avg_layout           = QtGui.QHBoxLayout()   # Container
        gb_avg_layout_bottom1   = QtGui.QHBoxLayout()   # next row...
        gb_avg_layout_bottom2   = QtGui.QHBoxLayout()

        # Create items
        self.avg_window_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.avg_window_slider.setTracking = False
        self.avg_window_slider.setMinimum(1)
        self.avg_window_slider.setMaximum(200)    # val*100ms, i.e max = 5000ms = 5s
        self.avg_window_slider.setValue(20)
        self.avg_window_slider.sliderReleased.connect(self.AverageWindowSliderReleased)
        self.avg_window_slider.valueChanged.connect(self.AverageWindowSliderMoved)

        self.avg_interval_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.avg_interval_slider.setTracking = False
        self.avg_interval_slider.setMinimum(1)
        self.avg_interval_slider.setMaximum(1024)  # val*16 i.e max = 8192 samples
        self.avg_interval_slider.setValue(4)
        self.avg_interval_slider.sliderReleased.connect(self.AverageIntervalSliderReleased)
        self.avg_interval_slider.valueChanged.connect(self.AverageIntervalSliderMoved)

        self.avg_window_label = EditableLabel(gb_avg_layout_bottom1, 2)
        self.avg_window_label.setText('2.00 s')
        self.avg_window_label.valueChanged.connect(self.AverageWindowValueChanged)

        self.avg_sample_num_label = EditableLabel(gb_avg_layout_bottom2, 1)
        self.avg_sample_num_label.setText('10')
        # self.avg_sample_num_label.valueChanged.connect(AverageWindowValueChanged)

        self.avg_run_button = QtGui.QPushButton('Stop')
        self.avg_run_button.clicked.connect(self.AvgRunButtonClicked)

        self.dut_power_button = QtGui.QPushButton('DUT Off')
        self.dut_power_button.clicked.connect(self.DUTPowerButtonPressed)

        self.calibration_btn = QtGui.QPushButton('Offset calibration')
        self.calibration_btn.clicked.connect(self.offset_calibration)

        # Set up groupbox with layouts
        gb_avg = QtGui.QGroupBox("Average")
        gb_avg_layout_bottom1.addWidget(QtGui.QLabel('Window:'))
        gb_avg_layout_bottom1.addWidget(self.avg_window_slider)
        gb_avg_layout_bottom1.addWidget(self.avg_window_label)
        gb_avg_layout_bottom1.addWidget(self.avg_run_button)

        gb_avg_layout.addLayout(gb_avg_layout_bottom1)
        gb_avg_layout.addLayout(gb_avg_layout_bottom2)
        gb_avg.setLayout(gb_avg_layout)
        dut_button_layout.addWidget(self.dut_power_button)
        # If you want to clutter the GUI with an offset button as well, uncomment
        # dut_button_layout.addWidget(self.calibration_btn)
        dut_button_layout.addWidget(gb_avg)
        top_layout.addLayout(dut_button_layout)
        # Return the groupbox object
        return top_layout

    def offset_calibration(self):
        self.plot_window.global_offset = 0.0
        # self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_DUT, 0])
        self.plot_window.calibrating = True
        self.plot_window.calibrating_done = False

    def trigger_settings(self):
        gb_trigger_layout           = QtGui.QVBoxLayout()   # Container
        gb_trigger_layout_top       = QtGui.QHBoxLayout()   # top row
        gb_trigger_layout_bottom   = QtGui.QHBoxLayout()   # next row
        gb_trigger_layout_bottom1   = QtGui.QHBoxLayout()   # next row
        gb_trigger_layout_bottom2   = QtGui.QHBoxLayout()   # next row

        # Create items
        self.triggerlevel_textbox = QtGui.QLineEdit()
        self.triggerlevel_textbox.returnPressed.connect(self.TriggerLevelPressedReturn)
        self.triggerlevel_textbox.setText(str(PlotData.trigger))

        self.trigger_single_button = QtGui.QPushButton('Single')
        self.trigger_single_button.clicked.connect(self.TriggerSingleButtonClicked)

        self.trigger_start_button = QtGui.QPushButton("Stop")
        self.trigger_start_button.clicked.connect(self.TriggerStartButtonClicked)

        self.trigger_window_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.trigger_window_slider.setTracking = False
        self.trigger_window_slider.setMinimum(370)
        self.trigger_window_slider.setMaximum(2048)
        self.trigger_window_slider.setValue(512)
        self.trigger_window_slider.sliderReleased.connect(self.TriggerWindowSliderReleased)
        self.trigger_window_slider.valueChanged.connect(self.TriggerWindowSliderMoved)
        self.trig_window_label = EditableLabel(gb_trigger_layout_bottom, 4)
        self.trig_window_label.setText('6.65 ms')
        self.trig_window_label.valueChanged.connect(self.TriggerWindowValueChanged)
        self.enable_ext_trigg_chkb = QtGui.QCheckBox()

        # Set up groupbox with layouts
        gb_trigger = QtGui.QGroupBox("Trigger")
        gb_trigger_layout_top.addWidget(self.trigger_single_button)
        gb_trigger_layout_top.addWidget(self.trigger_start_button)
        gb_trigger_layout_bottom.addWidget(QtGui.QLabel('Window:'))
        gb_trigger_layout_bottom.addWidget(self.trigger_window_slider)
        gb_trigger_layout_bottom.addWidget(self.trig_window_label)
        gb_trigger_layout_bottom1.addWidget(QtGui.QLabel('Trigger level:'))
        gb_trigger_layout_bottom1.addWidget(self.triggerlevel_textbox)
        gb_trigger_layout_bottom1.addWidget(QtGui.QLabel(str_uA))
        gb_trigger_layout_bottom2.addWidget(QtGui.QLabel('Enable external trigger'))

        gb_trigger_layout_bottom2.addWidget(self.enable_ext_trigg_chkb)
        self.enable_ext_trigg_chkb.setChecked(False)
        self.enable_ext_trigg_chkb.stateChanged.connect(self.external_trig_changed)
        
        gb_trigger_layout.addLayout(gb_trigger_layout_top)
        gb_trigger_layout.addLayout(gb_trigger_layout_bottom)
        gb_trigger_layout.addLayout(gb_trigger_layout_bottom1)
        gb_trigger_layout.addLayout(gb_trigger_layout_bottom2)

        gb_trigger.setLayout(gb_trigger_layout)

        # Return the groupbox object
        return gb_trigger

    def range_settings(self):
        # Create items
        range_drop_down = QtGui.QComboBox()

        range_drop_down.addItem("15uA")
        range_drop_down.addItem("1.5mA")
        range_drop_down.addItem("150mA")
        range_drop_down.addItem("Auto")
        range_drop_down.setCurrentIndex(3)
        range_drop_down.currentIndexChanged.connect(self.rangeChanged)

        # Set up groupbox with layouts
        gb_range = QtGui.QGroupBox("Range")
        gb_range_layout = QtGui.QVBoxLayout()
        gb_range_layout.addWidget(range_drop_down)
        gb_range.setLayout(gb_range_layout)

        # Return the groupbox object
        return gb_range

    def cursor_settings(self):
        gb_cursors_layout = QtGui.QVBoxLayout()
        curs_avg_box_layout = QtGui.QHBoxLayout()
        curs_avg_box_text_layout = QtGui.QVBoxLayout()
        curs_trig_box_text_layout = QtGui.QVBoxLayout()
        curs_trig_layout = QtGui.QHBoxLayout()

        bold = QtGui.QFont()
        bold.setBold(True)
        gb_cursors = QtGui.QGroupBox("Cursors")
        # gb_cursors.setFont(bold)

        curs_avg_box = QtGui.QGroupBox("Average window")
        self.curs_avg_enabled_checkb = QtGui.QCheckBox("Enabled")
        self.curs_avg_enabled_checkb.setChecked(True)
        self.curs_avg_enabled_checkb.stateChanged.connect(self.curs_avg_en_changed)
        self.curs_avg_rms_label = QtGui.QLabel("RMS: <b>0.00</b> [nA]")
        self.curs_avg_avg_label = QtGui.QLabel("AVG: <b>0.00</b> [nA]")
        self.curs_avg_cursx_label = QtGui.QLabel("X1: <b>1.00</b> [s] X2: <b>1.20</b> [s]")
        self.curs_avg_cursy_label = QtGui.QLabel("Y1: <b>0.00</b> [nA] Y2: <b>0.00</b> [nA]")
        self.curs_avg_cursy_label.setFixedWidth(180)
        self.curs_avg_delta_label = QtGui.QLabel("Cursor %s: <b>200.00</b> [ms]" % (str_delta))

        curs_avg_box_layout.addWidget(self.curs_avg_enabled_checkb)
        curs_avg_box_text_layout.addWidget(self.curs_avg_rms_label)

        curs_avg_box_text_layout.addWidget(self.curs_avg_avg_label)
        curs_avg_box_text_layout.addWidget(self.curs_avg_cursx_label)
        curs_avg_box_text_layout.addWidget(self.curs_avg_cursy_label)

        curs_avg_box_text_layout.addWidget(self.curs_avg_delta_label)
        curs_avg_box_layout.addLayout(curs_avg_box_text_layout)
        curs_avg_box.setLayout(curs_avg_box_layout)

        curs_trig_box = QtGui.QGroupBox("Trigger window")
        self.curs_trig_enabled_checkb = QtGui.QCheckBox("Enabled")
        self.curs_trig_enabled_checkb.stateChanged.connect(self.curs_trig_en_changed)
        self.curs_trig_enabled_checkb.setChecked(True)
        self.curs_trig_rms_label = QtGui.QLabel("RMS: <b>0.00</b> [nA]")
        self.curs_trig_avg_label = QtGui.QLabel("AVG: <b>0.00</b> [nA]")
        self.curs_trig_cursx_label = QtGui.QLabel("X1: <b>5.00</b> [ms] X2: <b>6.00</b> [ms]")
        self.curs_trig_cursy_label = QtGui.QLabel("Y1: <b>0.00</b> [nA] Y2: <b>0.00</b> [nA]")
        self.curs_trig_cursy_label.setFixedWidth(180)
        self.curs_trig_delta_label = QtGui.QLabel("Cursor %s: <b>3.00</b> [ms]" % (str_delta))

        curs_trig_layout.addWidget(self.curs_trig_enabled_checkb)
        curs_trig_box_text_layout.addWidget(self.curs_trig_rms_label)

        curs_trig_box_text_layout.addWidget(self.curs_trig_avg_label)
        curs_trig_box_text_layout.addWidget(self.curs_trig_cursx_label)
        curs_trig_box_text_layout.addWidget(self.curs_trig_cursy_label)

        curs_trig_box_text_layout.addWidget(self.curs_trig_delta_label)
        curs_trig_layout.addLayout(curs_trig_box_text_layout)
        curs_trig_box.setLayout(curs_trig_layout)

        gb_cursors_layout.addWidget(curs_avg_box)
        gb_cursors_layout.addWidget(curs_trig_box)
        gb_cursors.setLayout(gb_cursors_layout)

        return gb_cursors

    def edit_colors_button(self):
        btn = QtGui.QPushButton("Change graph color")
        btn.clicked.connect(self.plot_window.edit_colors)
        return btn

    def edit_bg_button(self):
        btn = QtGui.QPushButton("Change background color")
        btn.clicked.connect(self.plot_window.edit_bg)
        return btn

    def calibrate_offset_button(self):
        btn = QtGui.QPushButton("Calibrate")
        btn.clicked.connect(self.calibrate_button_clicked)
        return btn

    def statusbar(self):
        # Create label
        self.rms_label = QtGui.QLabel()
        status_font = QtGui.QFont("Arial", 10)
        self.rms_label.setFont(status_font)
        self.rms_label.setText("<b>max:</b> 0.00 <b>min:</b> 0.00 <b>rms:</b> 0.00 <b>avg:</b> 0.00 ")

        # Create the statusbar
        statusBar = QtGui.QStatusBar(self.settings_widget)
        statusBar.addPermanentWidget(self.rms_label)

        # Return the groupbox object
        return statusBar

    def vrefs(self):
        adjustments_layout = QtGui.QVBoxLayout()    # main layout

        vdd_layout = QtGui.QHBoxLayout()               # Layout for vdd slider
        vdd_gb = QtGui.QGroupBox("Voltage regulator")  # Groupbox for vdd

        vref_slider_layout = QtGui.QVBoxLayout()    #
        switches_layout = QtGui.QVBoxLayout()

        vref_on_layout = QtGui.QHBoxLayout()          # sublayout for vrefs
        vref_on_sliders_layout = QtGui.QHBoxLayout()  # For slider and label
        vref_on_labels_layout = QtGui.QVBoxLayout()   # For values

        vref_off_layout = QtGui.QHBoxLayout()          # Sublayout for vrefs off
        vref_off_sliders_layout = QtGui.QHBoxLayout()  # For slider and label
        vref_off_labels_layout = QtGui.QVBoxLayout()   # For values
        switches_gb = QtGui.QGroupBox("Switching points")

        self.vref_on_label_1 = QtGui.QLabel(str(38) + "m1")
        self.vref_on_label_2 = QtGui.QLabel(str(38) + "m2")

        self.vref_off_label_1 = QtGui.QLabel(str(2.34))
        self.vref_off_label_2 = QtGui.QLabel(str(2.34))
        self.vdd_label = QtGui.QLabel('3000mV')

        self.vref_off_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.vref_off_slider.setMinimum(100)
        self.vref_off_slider.setMaximum(400)
        self.vref_off_slider.setValue(234)
        self.vref_off_slider.setInvertedAppearance(True)
        self.vref_off_slider.sliderReleased.connect(self.vref_off_set)
        self.vref_off_slider.valueChanged.connect(self.vref_off_changed)

        self.vref_on_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.vref_on_slider.setMinimum(38)
        self.vref_on_slider.setMaximum(175)
        self.vref_on_slider.setValue(40)
        self.vref_on_slider.sliderReleased.connect(self.vref_on_set)
        self.vref_on_slider.valueChanged.connect(self.vref_on_changed)

        self.vdd_slider = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.vdd_slider.setMinimum(1850)
        self.vdd_slider.setMaximum(3600)
        self.vdd_slider.setValue(3000)
        self.vdd_slider.sliderReleased.connect(self.vdd_set)
        self.vdd_slider.valueChanged.connect(self.vdd_changed)

        vdd_layout.addWidget(QtGui.QLabel("VDD:"))
        vdd_layout.addWidget(self.vdd_slider)
        vdd_layout.addWidget(self.vdd_label)

        vref_on_sliders_layout.addWidget(QtGui.QLabel("Switch up  "))
        vref_on_sliders_layout.addWidget(self.vref_on_slider)

        vref_on_labels_layout.addWidget(self.vref_on_label_2)
        vref_on_labels_layout.addWidget(self.vref_on_label_1)

        vref_on_layout.addLayout(vref_on_sliders_layout)
        vref_on_layout.addLayout(vref_on_labels_layout)

        vref_off_sliders_layout.addWidget(QtGui.QLabel("Switch down"))
        vref_off_sliders_layout.addWidget(self.vref_off_slider)

        vref_off_labels_layout.addWidget(self.vref_off_label_1)
        vref_off_labels_layout.addWidget(self.vref_off_label_2)

        vref_off_layout.addLayout(vref_off_sliders_layout)
        vref_off_layout.addLayout(vref_off_labels_layout)

        switches_layout.addLayout(vref_on_layout)
        switches_layout.addLayout(vref_off_layout)
        vref_slider_layout.addLayout(vdd_layout)

        vdd_gb.setLayout(vref_slider_layout)
        switches_gb.setLayout(switches_layout)

        adjustments_layout.addWidget(vdd_gb)
        adjustments_layout.addWidget(switches_gb)

        return adjustments_layout

    def calibration_resistors(self):
        cal_res_gb = QtGui.QGroupBox("Resistor calibration")
        cal_res_layout = QtGui.QHBoxLayout()
        self.r_high_tb = QtGui.QLineEdit()
        self.r_mid_tb = QtGui.QLineEdit()
        self.r_lo_tb = QtGui.QLineEdit()
        self.cal_update_button = QtGui.QPushButton('Update')
        self.cal_reset_button = QtGui.QPushButton('Reset')

        self.r_high_tb.returnPressed.connect(self.update_cal_res)
        self.r_mid_tb.returnPressed.connect(self.update_cal_res)
        self.r_lo_tb.returnPressed.connect(self.update_cal_res)
        self.cal_update_button.clicked.connect(self.update_cal_res)
        self.cal_reset_button.clicked.connect(self.reset_cal_res)

        cal_res_layout.addWidget(QtGui.QLabel("Hi"))
        cal_res_layout.addWidget(self.r_high_tb)
        cal_res_layout.addWidget(QtGui.QLabel("Mid"))
        cal_res_layout.addWidget(self.r_mid_tb)
        cal_res_layout.addWidget(QtGui.QLabel("Lo"))
        cal_res_layout.addWidget(self.r_lo_tb)
        cal_res_layout.addWidget(self.cal_update_button)
        cal_res_layout.addWidget(self.cal_reset_button)

        cal_res_gb.setLayout(cal_res_layout)

        return cal_res_gb

    def update_cal_res(self):
        self.write_new_res(float(self.r_lo_tb.text()), float(self.r_mid_tb.text()), float(self.r_high_tb.text()))
        # print(float(self.r_lo_tb.text()), float(self.r_mid_tb.text()), float(self.r_high_tb.text()))

        PlotData.MEAS_RES_HI    = float(self.r_high_tb.text())
        PlotData.MEAS_RES_MID   = float(self.r_mid_tb.text())
        PlotData.MEAS_RES_LO    = float(self.r_lo_tb.text())

    def reset_cal_res(self):
        self.write_new_res(self.calibrated_res_lo, self.calibrated_res_mid, self.calibrated_res_hi)
        self.r_lo_tb.setText(str(self.calibrated_res_lo))
        self.r_mid_tb.setText(str(self.calibrated_res_mid))
        self.r_high_tb.setText(str(self.calibrated_res_hi))
        self.write_new_res(float(self.r_lo_tb.text()), float(self.r_mid_tb.text()), float(self.r_high_tb.text()))

        PlotData.MEAS_RES_HI    = float(self.r_high_tb.text())
        PlotData.MEAS_RES_MID   = float(self.r_mid_tb.text())
        PlotData.MEAS_RES_LO    = float(self.r_lo_tb.text())

    def calibrate_button_clicked(self):
        pass
        # self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_CALIBRATE_OFFSET])

    def set_trigger(self, trigger):
        PlotData.trigger_high = trigger >> 8
        PlotData.trigger_low = trigger & 0xFF
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TRIGGER_SET, PlotData.trigger_high, PlotData.trigger_low])

    def set_single(self, trigger):
        PlotData.trigger_high = trigger >> 8
        PlotData.trigger_low = trigger & 0xFF
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SINGLE_TRIG, PlotData.trigger_high, PlotData.trigger_low])

    def TriggerStartButtonClicked(self):
        if self.trigger_start_button.text() == 'Start':
            self.TriggerLevelPressedReturn()
        else:
            self.trigger_start_button.setText('Start')
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TRIG_STOP])

    def AvgRunButtonClicked(self):
        if self.avg_run_button.text() == 'Stop':
            self.avg_run_button.setText('Start')
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_STOP])
            print("Stopped average graph.")
        elif self.avg_run_button.text() == 'Start':
            self.avg_run_button.setText('Stop')
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RUN])
            print("Started average graph.")

    def DUTPowerButtonPressed(self):
        if self.dut_power_button.text() == 'DUT Off':
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_DUT, 0])
            self.dut_power_button.setText("DUT On")
        else:
            self.dut_power_button.setText("DUT Off")
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_DUT, 1])

    def TriggerSingleButtonClicked(self):
        self.trigger_start_button.setText('Start')
        self.trigger_start_button.setEnabled(False)
        self.trigger_single_button.setText('Waiting...')
        trigger_level = int(self.triggerlevel_textbox.text())
        self.set_single(trigger_level)
        print("Single run with trigger: %d%s" % (trigger_level, 'uA'))

    def TriggerLevelPressedReturn(self):
        self.trigger_start_button.setText("Stop")
        try:
            trigger_level = int(self.triggerlevel_textbox.text())
            self.set_trigger(trigger_level)
            print("Triggering at %d%s" % (trigger_level, 'uA'))
        except:
            print("Invalid trigger value (not an integer)")

    def show_calib_msg_box(self):
        # Start a threaded procedure to avoid invoking in on main thread
        thread = ShowInfoWindow('Information', 'Calibrating...')
        thread.show_calib_signal.connect(self._show_calib_msg_box)
        thread.start()

    def _show_calib_msg_box(self, title, text):
        self.msgBox.setWindowTitle(title)
        self.msgBox.setIconPixmap(QtGui.QPixmap('images\icon.ico'))
        self.msgBox.setText(text)
        self.msgBox.show()

    def close_calib_msg_box(self):
        thread = CloseInfoWindow()
        thread.close_calib_signal.connect(self._close_calib_msg_box)
        thread.start()

    def _close_calib_msg_box(self):
        self.msgBox.close()

    def TriggerWindowSliderReleased(self):
        self.TriggerWindowValueChanged()

    def TriggerWindowValueChanged(self):
        # Format the inserted text to float, cast to int and convert to bytes as required later
        try:
            self.trig_window_val = int(float(self.trig_window_label.text().split('ms')[0].replace(' ', '')) / (PlotData.trig_interval * 1000.0) + 1)
            self.trigger_window_slider.setValue(self.trig_window_val)
        except Exception as e:
            print(str(e))
            print(self.trig_window_label.text())
            sys.stdout.flush()

        PlotData.trig_timewindow = PlotData.trig_interval * self.trig_window_val
        PlotData.trigger_high = self.trig_window_val >> 8
        PlotData.trigger_low = self.trig_window_val & 0xFF
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TRIG_WINDOW_SET, PlotData.trigger_high, PlotData.trigger_low])

        self.trig_bufsize = int(PlotData.trig_timewindow / PlotData.trig_interval)
        PlotData.trig_x = np.linspace(0.0, PlotData.trig_timewindow, self.trig_bufsize)
        PlotData.trig_y = np.zeros(self.trig_bufsize, dtype=np.float)

        self.trig_window_label.setText('%5.2f ms' % ((PlotData.trig_timewindow * 1000)))
        sys.stdout.flush()

    def TriggerWindowSliderMoved(self, val):
        ''' This method is for previewing the value that will be set upon release '''
        value = PlotData.trig_interval * val
        self.trig_window_label.setText('%5.2f ms' % (value * 1000))
        sys.stdout.flush()

    def AverageWindowSliderReleased(self):
        self.AverageWindowValueChanged()

    def AverageWindowValueChanged(self):
        avg_window_val = float(self.avg_window_label.text().split(' ')[0])
        PlotData.avg_timewindow = (avg_window_val)
        self.avg_window_slider.setValue(avg_window_val * 10)

        PlotData.avg_bufsize  = int(PlotData.avg_timewindow / PlotData.avg_interval)
        PlotData.avg_x = np.linspace(0.0, PlotData.avg_timewindow, PlotData.avg_bufsize)
        PlotData.avg_y = np.zeros(PlotData.avg_bufsize, dtype=np.float)

        self.avg_window_label.setText('%.2f s' % (PlotData.avg_timewindow))
        sys.stdout.flush()

    def AverageWindowSliderMoved(self, val):
        ''' This method is for previewing the value that will be set upon release '''
        self.avg_window_label.setText('%.2f s' % (val / 10.0))

    def AverageIntervalSliderReleased(self):
        avg_samples_val = int(self.avg_sample_num_label.text())
        samples_high = (avg_samples_val / 10) >> 8
        samples_low  = (avg_samples_val / 10) & 0xFF
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_AVG_NUM_SET, samples_high, samples_low])

        PlotData.avg_interval   = PlotData.sample_interval * avg_samples_val
        PlotData.avg_bufsize  = int(PlotData.avg_timewindow / PlotData.avg_interval)
        PlotData.avg_x = np.linspace(0.0, PlotData.avg_timewindow, PlotData.avg_bufsize)
        PlotData.avg_y = np.zeros(PlotData.avg_bufsize, dtype=np.float)

    def AverageIntervalSliderMoved(self, val):
        self.avg_sample_num_label.setText('%d' % (val * 10))
        self.AverageIntervalSliderReleased()

    def curs_avg_en_changed(self, state):
        self.curs_avg_enabled = bool(state)
        if self.curs_avg_enabled:
            self.plot_window.avg_region.show()
        else:
            self.plot_window.avg_region.hide()

    def curs_trig_en_changed(self, state):
        self.curs_trig_enabled = bool(state)
        if self.curs_trig_enabled:
            self.plot_window.trig_region.show()
        else:
            self.plot_window.trig_region.hide()

    def external_trig_changed(self, state):
        self.external_trig_enabled = bool(state)
        self.trigger_start_button.setText('Start')
        if self.external_trig_enabled:
            self.triggerlevel_textbox.setEnabled(False)
            self.trigger_start_button.setEnabled(False)
            self.trigger_single_button.setEnabled(False)
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TRIG_STOP])
        else:
            self.trigger_single_button.setEnabled(True)
            self.trigger_start_button.setEnabled(True)
            self.triggerlevel_textbox.setEnabled(True)

        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TOGGLE_EXT_TRIG])

    def rangeChanged(self, val):
        if self.rtt is None:
            return

        if val == 0:
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RANGE_SET, 0])
            print("10uA range")
        elif val == 1:
            print("1mA range")
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RANGE_SET, 1])
        elif val == 2:
            print("100mA range")
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RANGE_SET, 2])
        elif val == 3:
            print("Auto range")
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RANGE_SET, 3])

        sys.stdout.flush()

    def avg_region_changed(self):
        # getRegion returns tuple of min max, not cursor 1 and 2
        i, j = self.plot_window.avg_region.getRegion()
        ival, iunit = self.sec_unit_determine(i)
        jval, junit = self.sec_unit_determine(j)
        deltaval, deltaunit = self.sec_unit_determine(j - i)

        if((ival >= 0) and (jval >= 0)):
            self.curs_avg_cursx_label.setText("X1: <b>%.2f</b> %s X2: <b>%.2f</b> %s" % (ival, iunit, jval, junit))
            self.curs_avg_delta_label.setText("Cursor %s: <b>%.2f</b> %s" % (str_delta, deltaval, deltaunit))

    def trig_region_changed(self):
        i = self.plot_window.trig_region.getRegion()[0]
        j = self.plot_window.trig_region.getRegion()[1]
        ival, iunit = self.sec_unit_determine(i)
        jval, junit = self.sec_unit_determine(j)
        deltaval, deltaunit = self.sec_unit_determine(j - i)

        if((ival >= 0) and (jval >= 0)):
            self.curs_trig_cursx_label.setText("X1: <b>%.2f</b> %s X2: <b>%.2f</b> %s" % (ival, iunit, jval, junit))
            self.curs_trig_delta_label.setText("Cursor %s: <b>%.2f</b> %s" % (str_delta, deltaval, deltaunit))

    def vdd_changed(self):
        ''' Update label, but don't transfer command '''
        self.vdd_label.setText(str(self.vdd_slider.value()) + "mV")

    def vdd_set(self):

        target_vdd = self.vdd_slider.value()

        steps = abs(target_vdd - self.m_vdd)

        if (steps > 350):
            if (target_vdd > self.m_vdd):
                for step in range(steps / 100):
                    new_step = target_vdd - (steps - step * 100)
                    vdd_high_byte = new_step >> 8
                    vdd_low_byte = new_step & 0xFF
                    self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SETVDD, vdd_high_byte, vdd_low_byte])
            elif (target_vdd < self.m_vdd):
                for step in range(steps / 100):
                    new_step = self.m_vdd - step * 100
                    vdd_high_byte = new_step >> 8
                    vdd_low_byte = new_step & 0xFF
                    self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SETVDD, vdd_high_byte, vdd_low_byte])
        else:
            vdd_high_byte = self.vdd_slider.value() >> 8
            vdd_low_byte = self.vdd_slider.value() & 0xFF
            self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SETVDD, vdd_high_byte, vdd_low_byte])
            # print("send %d" % self.vdd_slider.value())
        self.m_vdd = target_vdd

    def vref_on_changed(self):
        # print "vref_on_slider_value %f.2" % (self.vref_on_slider.value())
        self.isw_on_1 = self.vref_on_slider.value() / PlotData.MEAS_RES_LO
        self.isw_on_2 = self.vref_on_slider.value() / PlotData.MEAS_RES_MID

        self.vref_on_label_1.setText("LO: %.0f%s" % (self.isw_on_1 * 1000, "uA"))
        self.vref_on_label_2.setText("HI: %.2f%s" % (self.isw_on_2, "mA"))
        self.vref_off_changed()

    def vref_on_set(self):
        pot = 27000.0 * ((10.98194 * self.vref_on_slider.value() / 1000) / 0.41 - 1)
        # print(self.vref_on_slider.value())
        # print(pot)

        vref_on_msb = int(pot / 2) >> 8
        vref_on_lsb = int(pot / 2) & 0xFF

        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SETVREFHI, vref_on_msb, vref_on_lsb])

    def vref_off_changed(self):
        hysteresis = (self.vref_off_slider.value() / 100.0)
        # print(hysteresis)
        i_sw_on_3 = self.vref_on_slider.value()
        i_sw_off_1 = self.isw_on_2 / 16.3 / hysteresis
        i_sw_off_2 = i_sw_on_3 / 16.3 / hysteresis

        self.vref_off_label_1.setText(str("HI: %.2fmA" % (i_sw_off_1)))
        self.vref_off_label_2.setText(str("LO: %.2fuA" % (i_sw_off_2)))

    def vref_off_set(self):
        pot = 2000.0 * (16.3 * self.vref_off_slider.value() / 100.0 - 1) - 30000.0
        vref_off_msb = int(pot / 2) >> 8
        vref_off_lsb = int(pot / 2) & 0xFF

        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_SETVREFLO, vref_off_msb, vref_off_lsb])

        # print("Sent vref lo pot: " + str(pot))
        # print("Sent vref lo: " + str(self.vref_off_slider.value()))

    def sec_unit_determine(self, timestamp):
        val = 0
        unit = "[s]"
        if timestamp > 1:
            val = timestamp
            unit = "[s]"
        elif timestamp >= 1.0e-3:
            val = timestamp * 1.0e3
            unit = "[ms]"
        elif (timestamp < 1.0e-3) & (timestamp >= 1.0e-6):
            val = timestamp * 1.0e6
            unit = u'[\u03bcs]'
        else:
            val = timestamp * 1.0e9
            unit = "[ns]"
        return val, unit

    def unit_determine(self, current_A):
        val = 0
        unit = "mA"
        if current_A >= 1.0e-3:
            val = current_A * 1.0e3
            unit = "[mA]"
        elif (current_A < 1.0e-3) & (current_A >= 1.0e-6):
            val = current_A * 1.0e6
            unit = str_uA
        elif (current_A < 1.0e-6) & (current_A > -1.0e-6):
            val = current_A * 1.0e9
            unit = "[nA]"

        elif (current_A <= -1.0e-6) & (current_A > -1.0e-3):
            val = current_A * 1.0e6
            unit = str_uA
        elif (current_A <= -1.0e-3):
            val = current_A * 1.0e3
            unit = "[mA]"
        else:
            pass  # Handle unit determine error

        return val, unit

    total_avg_consump = 0       
    avg_iteration_numb = 0

    def update_status(self):

        global avg_timeout

        _max = max(PlotData.avg_y)
        _min = min(PlotData.avg_y)
        _rms = rms_flat(PlotData.avg_y)
        _avg = np.average(PlotData.avg_y)

        # print(_avg,    _min,    _max )
        self.total_avg_consump += _avg
        self.avg_iteration_numb += 1
        mAs_avg_result = self.total_avg_consump*self.avg_iteration_numb*avg_timeout/1e+6/3600

        if self.avg_iteration_numb % 10 == 1:
            tmp_time = time.strftime('%H:%M:%S', time.localtime())
            print(mAs_avg_result, "mAh", tmp_time, end="\t\r")
        

        max_val, max_unit = self.unit_determine(_max)
        min_val, min_unit = self.unit_determine(_min)
        rms_val, rms_unit = self.unit_determine(_rms)
        avg_val, avg_unit = self.unit_determine(_avg)

        status_font = QtGui.QFont("Arial", 8)
        self.rms_label.setFont(status_font)
        self.rms_label.setText("max: <b>%.2f</b> %s min: <b>%.2f</b> %s rms: <b>%.2f</b> %s avg: <b>%.2f</b> %s"
                               % (max_val, max_unit, min_val, min_unit, rms_val, rms_unit, avg_val, avg_unit))
        self.plot_window.trig_curve.setData(PlotData.trig_x, PlotData.trig_y)

        if self.curs_avg_enabled:
            samples_per_us = len(PlotData.avg_x) / PlotData.avg_timewindow  # us
            curs1, curs2 = self.plot_window.avg_region.getRegion()
            byte_position_curs1 = int(samples_per_us * curs1)
            byte_position_curs2 = int(samples_per_us * curs2)

            try:
                if((byte_position_curs1 < 0) or (byte_position_curs2 < 0)):
                    raise
                curs_rms_val, curs_rms_unit = self.unit_determine(rms_flat(PlotData.avg_y[byte_position_curs1:byte_position_curs2]))
                self.curs_avg_rms_label.setText("RMS: <b>%.2f</b> %s" % (curs_rms_val, curs_rms_unit))
                curs_avg_val, curs_avg_unit = self.unit_determine(np.average(PlotData.avg_y[byte_position_curs1:byte_position_curs2]))
                self.curs_avg_avg_label.setText("AVG: <b>%.2f</b> %s" % (curs_avg_val, curs_avg_unit))
                curs1_y_val, curs1_y_unit = self.unit_determine(PlotData.avg_y[byte_position_curs1])
                curs2_y_val, curs2_y_unit = self.unit_determine(PlotData.avg_y[byte_position_curs2])
                self.curs_avg_cursy_label.setText("Y1: <b>%5.2f</b> %s Y2: <b>%5.2f</b> %s" % (curs1_y_val, curs1_y_unit, curs2_y_val, curs2_y_unit))
            except:
                self.curs_avg_rms_label.setText("RMS: <b>N/A (out of bounds)</b>")
                self.curs_avg_avg_label.setText("AVG: <b>N/A (out of bounds)</b>")

        if self.curs_trig_enabled:
            samples_per_us = len(PlotData.trig_x) / PlotData.trig_timewindow  # us
            curs1, curs2 = self.plot_window.trig_region.getRegion()
            byte_position_curs1 = int(samples_per_us * curs1)
            byte_position_curs2 = int(samples_per_us * curs2)

            try:
                if((byte_position_curs1 < 0) or (byte_position_curs2 < 0)):
                    raise

                curs1_y_val, curs1_y_unit = self.unit_determine(PlotData.trig_y[byte_position_curs1])
                curs2_y_val, curs2_y_unit = self.unit_determine(PlotData.trig_y[byte_position_curs2])

                curs_rms_val, curs_rms_unit = self.unit_determine(rms_flat(PlotData.trig_y[byte_position_curs1:byte_position_curs2]))
                curs_avg_val, curs_avg_unit = self.unit_determine(np.average(PlotData.trig_y[byte_position_curs1:byte_position_curs2]))

                self.curs_trig_rms_label.setText("RMS: <b>%.2f</b> %s" % (curs_rms_val, curs_rms_unit))
                self.curs_trig_avg_label.setText("AVG: <b>%.2f</b> %s" % (curs_avg_val, curs_avg_unit))
                self.curs_trig_cursy_label.setText("Y1: <b>%.2f</b> %s Y2: <b>%.2f</b> %s" % (curs1_y_val, curs1_y_unit, curs2_y_val, curs2_y_unit))
            except:
                self.curs_trig_rms_label.setText("RMS: <b>N/A (out of bounds)</b>")
                self.curs_trig_avg_label.setText("AVG: <b>N/A (out of bounds)</b>")

        self.plot_window.trig_curve.setData(PlotData.trig_x, PlotData.trig_y)


class pms_plotter():
    def __init__(self):
        # This app instance must be constructed before all other elements are added
        self.calibrating = False
        self.calibrating_done = False
        self.global_offset = 0.0
        self.setup_measurement_regions()
        pg.setConfigOption('background', 'k')  # Set white background
        self.gw = pg.GraphicsWindow()

        self.gw.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.gw.destroyed.connect(self.destroyedEvent)
        ico = QtGui.QIcon('images\icon.ico')
        self.gw.setWindowIcon(ico)

        self.gw.move(450, 50)
        self.gw.setWindowTitle('Plots - Power Profiler Kit')

        self.settings = SettingsWindow(PlotData, self)
        self.gw.resize((self.gw.width()), (self.settings.settings_widget.height()))

        # Need to connect these signals after the settings instance is created
        self.avg_region.sigRegionChanged.connect(self.settings.avg_region_changed)
        self.trig_region.sigRegionChanged.connect(self.settings.trig_region_changed)

        try:
            self.rtt = rtt.rtt(self.rtt_handler)
        except:
            print("Unable to connect to the PPK, check debugger connection and make sure pynrfjprog is up to date.")
            exit()
        self.settings.set_rtt_instance(self.rtt)
        self.setup_plot_window()

    def edit_colors(self):
        color = QtGui.QColorDialog.getColor()
        if color.isValid():
            self.trig_curve.setPen(color)
            self.avg_curve.setPen(color)

    def edit_bg(self):
        bg = QtGui.QColorDialog.getColor()
        if bg.isValid():
            self.gw.setBackground(bg)

    def destroyedEvent(self):
        try:
            QtGui.QApplication.quit()
        except:
            pass
        QtGui.QApplication.quit()

    def setup_measurement_regions(self):
        # Cursor with window for calculating avereages
        region_brush = QtGui.QBrush(QtGui.QColor(255, 255, 255, 20))
        self.trig_region = pg.LinearRegionItem()
        # Cursor with window for calculating avereages
        self.avg_region = pg.LinearRegionItem()
        for line in self.trig_region.lines:
            line.setPen(255, 80, 80, 85, width=3)
            line.setHoverPen(255, 255, 255, 100, width=3)
        for line in self.avg_region.lines:
            line.setPen(255, 80, 80, 85, width=3)
            line.setHoverPen(255, 255, 255, 100, width=3)
        self.trig_region.setBrush(region_brush)
        self.avg_region.setBrush(region_brush)
        self.trig_region.setZValue(10)
        # Set cursors at 5 and 6 ms
        self.trig_region.setRegion([0.001, 0.004])

        self.avg_region.setZValue(10)
        # Set cursors at 5 and 6 ms
        self.avg_region.setRegion([0.5, 0.9])

    def setup_plot_window(self):
        self.avg_plot = self.gw.addPlot(title='Average', row=0, col=1, rowspan=1, colspan=1)
        trig_plot = self.gw.addPlot(title='Trigger', row=1, col=1, rowspan=1, colspan=1)

        self.avg_plot.setLabel('left', 'current', 'A')
        self.avg_plot.setLabel('bottom', 'time', 's')
        self.avg_plot.showGrid(x=True, y=True)

        trig_plot.setLabel('left', 'current', 'A')
        trig_plot.setLabel('bottom', 'time', 's')
        trig_plot.showGrid(x=True, y=True)

        # Add the LinearRegionItem to the ViewBox, but tell the ViewBox to exclude this
        # item when doing auto-range calculations.
        self.avg_plot.addItem(self.avg_region, ignoreBounds=True)
        trig_plot.addItem(self.trig_region, ignoreBounds=True)
        # Create the curve for average data (top graph)
        self.avg_curve = self.avg_plot.plot(PlotData.avg_x, PlotData.avg_y)
        # Create the curve for trigger data (bottom graph)
        self.trig_curve = trig_plot.plot(PlotData.trig_x, PlotData.trig_y)

        # Bools for checking if we should update the curve when the update timer triggers
        self.update_trig_curve = False
        self.update_avg_curve = False

    def start(self, run=True):
        ''' Send trigger value and start to firmware.
            Starts timers for updating graphs and calculations.
        '''
        global avg_timeout

        # First we need to read out the calibrated measurmement R-values
        try:
            data = self.rtt.nrfjprog.rtt_read(0, 200)
            prod_data = data.split("USER SET ")[0]
            PlotData.MEAS_RES_LO  = float(prod_data.split("R1:")[1].split(" R2")[0])
            PlotData.MEAS_RES_MID = float(prod_data.split("R2:")[1].split(" R3")[0])
            PlotData.MEAS_RES_HI = float(prod_data.split("R3:")[1].split("Board ID ")[0])
            self.settings.board_id = str(prod_data.split("Board ID ")[1])
            print("Board ID: " + self.settings.board_id)
            self.settings.calibrated_res_lo = PlotData.MEAS_RES_LO
            self.settings.calibrated_res_mid = PlotData.MEAS_RES_MID
            self.settings.calibrated_res_hi = PlotData.MEAS_RES_HI
        except:
            print("Initialization failed, could not read calibration values.")
            exit()

        if('USER SET' in data):
            user_data = data.split("USER SET ")[1].split("Refs")[0]
            PlotData.MEAS_RES_LO  = float(user_data.split("R1:")[1].split(" R2")[0])
            PlotData.MEAS_RES_MID = float(user_data.split("R2:")[1].split(" R3")[0])
            PlotData.MEAS_RES_HI = float(user_data.split("R3:")[1].split("Board ID ")[0])

        try:
            refs_data = data.split("Refs ")[1]
        except:
            print("Corrupted data received from PPK, please reflash the PPK.")
            exit()
        PlotData.vref_hi = refs_data.split("HI: ")[1].split(" LO")[0]
        PlotData.vref_lo = refs_data.split("LO: ")[1]
        PlotData.vdd     = refs_data.split("VDD: ")[1].split(" HI")[0]
        self.settings.m_vdd = int(PlotData.vdd)

        self.settings.vdd_slider.setSliderPosition(int(PlotData.vdd))
        self.settings.vref_on_slider.setSliderPosition(int(((int(PlotData.vref_hi) * 2 / 27000.0) + 1) * (0.41 / 10.98194) * 1000))
        self.settings.vref_off_slider.setSliderPosition((((int(PlotData.vref_lo) * 2 + 30000) / 2000.0 + 1) / 16.3) * 100)

        self.settings.r_high_tb.setText(str(PlotData.MEAS_RES_HI))
        self.settings.r_mid_tb.setText(str(PlotData.MEAS_RES_MID))
        self.settings.r_lo_tb.setText(str(PlotData.MEAS_RES_LO))

        self.rtt.start()
        # Trigger trigger window update, since production firmware uses wrong window value
        self.settings.TriggerWindowValueChanged()
        # Write the initial trigger value, set in PlotData
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_TRIGGER_SET, PlotData.trigger_high, PlotData.trigger_low])
        # Timer to update graphs, continous shot
        self.calibrating = True

        timer = pg.QtCore.QTimer(self.gw)
        timer.timeout.connect(self.update)
        timer.start(1)  # 1ms
        # Timer to update rms value
        timer_rms = pg.QtCore.QTimer(self.gw)
        timer_rms.timeout.connect(self.settings.update_status)
        timer_rms.start(avg_timeout)  # 1s
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_RUN])
        self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_AVG_NUM_SET, 0x00, 1])

    def rtt_handler(self, data):
        ''' All measurments arrive here. 4 bytes for avg window, 16 bytes for trigger window '''

        if(not self.calibrating_done):
            if not hasattr(self, "calibration_counter"):
                self.calibration_counter = 10000  # it doesn't exist yet, so initialize it
                self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_DUT, 0])
                self.settings.show_calib_msg_box()

            if(self.calibrating):
                if(self.calibration_counter != 0):
                    self.calibration_counter = self.calibration_counter - 1
                    if (len(data) == 4):

                        s = ''.join([chr(b) for b in data])
                        f = struct.unpack('f', s)[0]        # Get the uA value

                        PlotData.avg_y[:-1] = PlotData.avg_y[1:]  # shift data in the array one sample left
                        PlotData.avg_y[-1] = f / 1e6

                        self.update_avg_curve = True

                else:
                    # Got all the samples
                    self.calibrating = False
            else:
                self.calibrating_done = True
                self.settings.close_calib_msg_box()
                self.global_offset = np.average(PlotData.avg_y[1000:8000])
                self.rtt.write_stuffed([RTT_COMMANDS.RTT_CMD_DUT, 1])
                del self.calibration_counter
                PlotData.avg_y = np.zeros(PlotData.avg_bufsize, dtype=np.float)

        if (len(data) == 4):

            s = ''.join([chr(b) for b in data])
            f = struct.unpack('f', s)[0]
            PlotData.avg_y[:-1] = PlotData.avg_y[1:]  # shift data in the array one sample left
            PlotData.avg_y[-1] = f / 1e6 - self.global_offset
            #print(data[0])

            self.update_avg_curve = True
        else:  # Trigger data received
            for i in range(0, len(data), 2):
                if (i + 1) < len(data):
                    tmp = np.uint16((data[i + 1] << 8) + data[i])
                    PlotData.current_meas_range = (tmp & MEAS_RANGE_MSK) >> MEAS_RANGE_POS
                    adc_val = (tmp & MEAS_ADC_MSK) >> MEAS_ADC_POS
                    sample_A = 0.0

                    if PlotData.current_meas_range == MEAS_RANGE_LO:
                        sample_A = adc_val * (ADC_REF / (ADC_GAIN * ADC_MAX * PlotData.MEAS_RES_LO))
                        sample_A = sample_A - self.global_offset

                    elif PlotData.current_meas_range == MEAS_RANGE_MID:
                        sample_A = adc_val * (ADC_REF / (ADC_GAIN * ADC_MAX * PlotData.MEAS_RES_MID))
                    elif PlotData.current_meas_range == MEAS_RANGE_HI:
                        sample_A = adc_val * (ADC_REF / (ADC_GAIN * ADC_MAX * PlotData.MEAS_RES_HI))
                    elif PlotData.current_meas_range == MEAS_RANGE_INVALID:
                        print("Range INVALID")
                    elif PlotData.current_meas_range == MEAS_RANGE_NONE:
                        print("Range not detected")

                    PlotData.trig_y[:-1] = PlotData.trig_y[1:]  # shift data in the array one sample left
                    # PlotData.trig_y[-1] = sample_A

                    # if(sample_A < 50e-6):
                    #     PlotData.trig_y[-1] = (0.9587*sample_A + 1.4395)/1e6 # We get the result in uA
                    # else:
                    PlotData.trig_y[-1] = sample_A
            self.update_trig_curve = True

    # update plots
    def update(self):
        sys.stdout.flush()
        if self.update_trig_curve:
            self.settings.trigger_single_button.setText("Single")
            if (not self.settings.external_trig_enabled):
                self.settings.trigger_start_button.setEnabled(True)
            self.trig_curve.setData(PlotData.trig_x, PlotData.trig_y)
            self.update_trig_curve = False

        if self.update_avg_curve:
            self.avg_curve.setData(PlotData.avg_x, PlotData.avg_y)
            self.update_avg_curve = False

# Start Qt event loop unless running in interactive mode or using pyside.
if __name__ == '__main__':
    ''' Check that python version is correct '''
    arch = platform.architecture()[0]

    ''' Check that packages are up to date '''
    print("Checking installed packages")
    print("pyside:\t\t %s" % PySide.__version__)
    print("pyqtgraph:\t %s" % pg.__version__)
    print("numpy:\t\t %s" % np.__version__)
    print("pynrfjprog:\t %s" % pynrfjprog.__version__)

    if ((PySide.__version__[0] != '1') or (PySide.__version__[2] != '2')):
        print("Warning: The software is tested with PySide 1.2.4, and may not work with your version (%s)" % PySide.__version__)
    if ((pg.__version__[0] != '0') or (pg.__version__[2] != '9')):
        print("Warning: The software is tested with PyQtGraph 0.9.10, and may not work with your version (%s)" % pg.__version__)
    if ((np.__version__[0] != '1') or (np.__version__[2] != '9')):
        print("Warning: The software is tested with np 1.9.2, and may not work with your version (%s)" % np.__version__)

    plotter = pms_plotter()
    plotter.start()

    if (sys.flags.interactive != 1) or not hasattr(QtCore, 'PYQT_VERSION'):
        QtGui.QApplication.instance().exec_()
