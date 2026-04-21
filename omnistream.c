#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <string.h>
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
#include <libavutil/hwcontext.h>
#include <libavutil/opt.h>
#include <libavutil/imgutils.h>
#include <libavutil/error.h>

typedef struct {
    PyObject_HEAD
    AVFormatContext *fmt_ctx;
    AVCodecContext *enc_ctx;
    AVBufferRef *hw_device_ctx;
    AVBufferRef *hw_frames_ref;
    struct SwsContext *sws_ctx;
    AVFrame *sw_frame;
    AVFrame *hw_frame;
    AVPacket *pkt;
    int width;
    int height;
    int fps;
    int bitrate;
    int initialized;
    char last_error[1024];
} Streamer;

static void set_error(Streamer *self, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(self->last_error, sizeof(self->last_error), fmt, args);
    va_end(args);
}

static int fail(Streamer *self, const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(self->last_error, sizeof(self->last_error), fmt, args);
    va_end(args);
    PyErr_SetString(PyExc_RuntimeError, self->last_error);
    return -1;
}

static void streamer_cleanup(Streamer *self) {
    if (self->sws_ctx) sws_freeContext(self->sws_ctx);
    if (self->sw_frame) av_frame_free(&self->sw_frame);
    if (self->hw_frame) av_frame_free(&self->hw_frame);
    if (self->pkt) av_packet_free(&self->pkt);
    if (self->enc_ctx) avcodec_free_context(&self->enc_ctx);
    if (self->fmt_ctx) {
        if (!(self->fmt_ctx->oformat->flags & AVFMT_NOFILE))
            avio_closep(&self->fmt_ctx->pb);
        avformat_free_context(self->fmt_ctx);
    }
    if (self->hw_device_ctx) av_buffer_unref(&self->hw_device_ctx);
    self->initialized = 0;
}

static int streamer_init(Streamer *self, PyObject *args, PyObject *kwargs) {
    const char *rtmp_url = NULL;
    int width = 1920, height = 1080, fps = 30, bitrate = 4000;

    static char *kwlist[] = {"url", "width", "height", "fps", "bitrate", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s|iiii", kwlist,
                                     &rtmp_url, &width, &height, &fps, &bitrate))
        return -1;

    self->width = width;
    self->height = height;
    self->fps = fps;
    self->bitrate = bitrate * 1000;
    self->initialized = 0;
    self->fmt_ctx = NULL;
    self->enc_ctx = NULL;
    self->hw_device_ctx = NULL;
    self->hw_frames_ref = NULL;
    self->sws_ctx = NULL;
    self->sw_frame = NULL;
    self->hw_frame = NULL;
    self->pkt = NULL;

    int ret;

    /* 1. Open VAAPI device */
    ret = av_hwdevice_ctx_create(&self->hw_device_ctx, AV_HWDEVICE_TYPE_VAAPI,
                                 "/dev/dri/renderD128", NULL, 0);
    if (ret < 0)
        return fail(self, "Failed to open VAAPI device: %s", av_err2str(ret));

    /* 2. Create hardware frames context */
    AVHWFramesContext *frames_ctx;
    self->hw_frames_ref = av_hwframe_ctx_alloc(self->hw_device_ctx);
    if (!self->hw_frames_ref)
        return fail(self, "Failed to alloc hw frame context");

    frames_ctx = (AVHWFramesContext *)self->hw_frames_ref->data;
    frames_ctx->format = AV_PIX_FMT_VAAPI;
    frames_ctx->sw_format = AV_PIX_FMT_NV12;
    frames_ctx->width = width;
    frames_ctx->height = height;

    ret = av_hwframe_ctx_init(self->hw_frames_ref);
    if (ret < 0)
        return fail(self, "Failed to init hw frame context: %s", av_err2str(ret));

    /* 3. Setup encoder */
    const AVCodec *codec = avcodec_find_encoder_by_name("h264_vaapi");
    if (!codec)
        return fail(self, "h264_vaapi encoder not found");

    self->enc_ctx = avcodec_alloc_context3(codec);
    if (!self->enc_ctx)
        return fail(self, "Failed to alloc encoder context");

    self->enc_ctx->width = width;
    self->enc_ctx->height = height;
    self->enc_ctx->time_base = (AVRational){1, fps};
    self->enc_ctx->framerate = (AVRational){fps, 1};
    self->enc_ctx->pix_fmt = AV_PIX_FMT_VAAPI;
    self->enc_ctx->bit_rate = self->bitrate;
    self->enc_ctx->rc_max_rate = self->bitrate;
    self->enc_ctx->rc_buffer_size = self->bitrate * 2;
    self->enc_ctx->gop_size = fps * 2;
    self->enc_ctx->keyint_min = fps * 2;
    self->enc_ctx->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;

    av_opt_set_int(self->enc_ctx, "bf", 0, 0);

    self->enc_ctx->hw_frames_ctx = av_buffer_ref(self->hw_frames_ref);
    if (!self->enc_ctx->hw_frames_ctx)
        return fail(self, "Failed to ref hw frames ctx");

    ret = avcodec_open2(self->enc_ctx, codec, NULL);
    if (ret < 0)
        return fail(self, "Failed to open encoder: %s", av_err2str(ret));

    /* 4. Setup swscale for BGRA -> NV12 */
    self->sws_ctx = sws_getContext(width, height, AV_PIX_FMT_BGRA,
                                   width, height, AV_PIX_FMT_NV12,
                                   SWS_FAST_BILINEAR, NULL, NULL, NULL);
    if (!self->sws_ctx)
        return fail(self, "Failed to create swscale context");

    /* 5. Allocate software frame for swscale output */
    self->sw_frame = av_frame_alloc();
    if (!self->sw_frame)
        return fail(self, "Failed to alloc sw frame");

    self->sw_frame->format = AV_PIX_FMT_NV12;
    self->sw_frame->width = width;
    self->sw_frame->height = height;
    ret = av_frame_get_buffer(self->sw_frame, 32);
    if (ret < 0)
        return fail(self, "Failed to alloc sw frame buffer: %s", av_err2str(ret));

    /* 6. Allocate hardware frame for encoding input */
    self->hw_frame = av_frame_alloc();
    if (!self->hw_frame)
        return fail(self, "Failed to alloc hw frame");

    ret = av_hwframe_get_buffer(self->hw_frames_ref, self->hw_frame, 0);
    if (ret < 0)
        return fail(self, "Failed to alloc hw frame buffer: %s", av_err2str(ret));

    /* 7. Allocate packet */
    self->pkt = av_packet_alloc();
    if (!self->pkt)
        return fail(self, "Failed to alloc packet");

    /* 8. Open output */
    int use_null_output = (strcmp(rtmp_url, "/dev/null") == 0 || strcmp(rtmp_url, "null") == 0);

    if (!use_null_output) {
        ret = avformat_alloc_output_context2(&self->fmt_ctx, NULL, "flv", rtmp_url);
        if (ret < 0)
            return fail(self, "Failed to alloc output context: %s", av_err2str(ret));

        AVStream *stream = avformat_new_stream(self->fmt_ctx, NULL);
        if (!stream)
            return fail(self, "Failed to create output stream");

        stream->time_base = self->enc_ctx->time_base;

        ret = avcodec_parameters_from_context(stream->codecpar, self->enc_ctx);
        if (ret < 0)
            return fail(self, "Failed to copy codec params: %s", av_err2str(ret));

        if (!(self->fmt_ctx->oformat->flags & AVFMT_NOFILE)) {
            ret = avio_open(&self->fmt_ctx->pb, rtmp_url, AVIO_FLAG_WRITE);
            if (ret < 0)
                return fail(self, "Failed to open RTMP output %s: %s", rtmp_url, av_err2str(ret));
        }

        ret = avformat_write_header(self->fmt_ctx, NULL);
        if (ret < 0)
            return fail(self, "Failed to write header: %s", av_err2str(ret));
    }

    self->initialized = 1;
    return 0;
}

static PyObject *streamer_send_frame(Streamer *self, PyObject *args) {
    Py_buffer buf;
    if (!PyArg_ParseTuple(args, "y*", &buf))
        return NULL;

    if (!self->initialized) {
        set_error(self, "Streamer not initialized");
        PyBuffer_Release(&buf);
        Py_RETURN_FALSE;
    }

    int expected_size = self->width * self->height * 4;
    if (buf.len != expected_size) {
        set_error(self, "Frame size mismatch: got %d, expected %d", (int)buf.len, expected_size);
        PyBuffer_Release(&buf);
        Py_RETURN_FALSE;
    }

    int ret;

    /* 1. Convert BGRA to NV12 in software */
    const uint8_t *src_data[1] = {(const uint8_t *)buf.buf};
    const int src_linesize[1] = {self->width * 4};

    sws_scale(self->sws_ctx, src_data, src_linesize, 0, self->height,
              self->sw_frame->data, self->sw_frame->linesize);

    PyBuffer_Release(&buf);

    /* 2. Transfer NV12 to VAAPI hardware frame */
    ret = av_hwframe_transfer_data(self->hw_frame, self->sw_frame, 0);
    if (ret < 0) {
        set_error(self, "Failed to transfer to hw frame: %s", av_err2str(ret));
        Py_RETURN_FALSE;
    }

    static int64_t pts_counter = 0;
    self->hw_frame->pts = pts_counter++;

    /* 3. Encode */
    ret = avcodec_send_frame(self->enc_ctx, self->hw_frame);
    if (ret < 0) {
        set_error(self, "Failed to send frame to encoder: %s", av_err2str(ret));
        Py_RETURN_FALSE;
    }

    /* 4. Read packets and mux */
    while (ret >= 0) {
        ret = avcodec_receive_packet(self->enc_ctx, self->pkt);
        if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) {
            break;
        } else if (ret < 0) {
            set_error(self, "Encoder error: %s", av_err2str(ret));
            Py_RETURN_FALSE;
        }

        if (self->fmt_ctx) {
            self->pkt->stream_index = 0;
            av_packet_rescale_ts(self->pkt, self->enc_ctx->time_base,
                                 self->fmt_ctx->streams[0]->time_base);

            ret = av_interleaved_write_frame(self->fmt_ctx, self->pkt);
            av_packet_unref(self->pkt);

            if (ret < 0) {
                set_error(self, "Failed to write frame: %s", av_err2str(ret));
                Py_RETURN_FALSE;
            }
        } else {
            av_packet_unref(self->pkt);
        }
    }

    Py_RETURN_TRUE;
}

static PyObject *streamer_flush(Streamer *self, PyObject *Py_UNUSED(ignored)) {
    if (!self->initialized) {
        set_error(self, "Streamer not initialized");
        Py_RETURN_FALSE;
    }

    int ret = avcodec_send_frame(self->enc_ctx, NULL);
    if (ret < 0) {
        set_error(self, "Flush send error: %s", av_err2str(ret));
        Py_RETURN_FALSE;
    }

    while (1) {
        ret = avcodec_receive_packet(self->enc_ctx, self->pkt);
        if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
        if (ret < 0) {
            set_error(self, "Flush receive error: %s", av_err2str(ret));
            Py_RETURN_FALSE;
        }

        if (self->fmt_ctx) {
            self->pkt->stream_index = 0;
            av_packet_rescale_ts(self->pkt, self->enc_ctx->time_base,
                                 self->fmt_ctx->streams[0]->time_base);
            ret = av_interleaved_write_frame(self->fmt_ctx, self->pkt);
            av_packet_unref(self->pkt);
            if (ret < 0) {
                set_error(self, "Flush write error: %s", av_err2str(ret));
                Py_RETURN_FALSE;
            }
        } else {
            av_packet_unref(self->pkt);
        }
    }

    if (self->fmt_ctx) {
        ret = av_write_trailer(self->fmt_ctx);
        if (ret < 0) {
            set_error(self, "Write trailer error: %s", av_err2str(ret));
            Py_RETURN_FALSE;
        }
    }

    Py_RETURN_TRUE;
}

static PyObject *streamer_get_error(Streamer *self, PyObject *Py_UNUSED(ignored)) {
    return PyUnicode_FromString(self->last_error);
}

static void streamer_dealloc(Streamer *self) {
    streamer_cleanup(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyMethodDef Streamer_methods[] = {
    {"send_frame", (PyCFunction)streamer_send_frame, METH_VARARGS, "Send a BGRA frame for encoding and RTMP push"},
    {"flush", (PyCFunction)streamer_flush, METH_NOARGS, "Flush encoder and write trailer"},
    {"get_error", (PyCFunction)streamer_get_error, METH_NOARGS, "Get last error message"},
    {NULL}
};

static PyTypeObject StreamerType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "omnistream.Streamer",
    .tp_doc = "Hardware-accelerated RTMP streamer using VAAPI",
    .tp_basicsize = sizeof(Streamer),
    .tp_itemsize = 0,
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    .tp_new = PyType_GenericNew,
    .tp_init = (initproc)streamer_init,
    .tp_dealloc = (destructor)streamer_dealloc,
    .tp_methods = Streamer_methods,
};

static PyModuleDef omnistream_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "omnistream",
    .m_doc = "Hardware-accelerated RTMP streaming via libavformat + VAAPI",
    .m_size = -1,
};

PyMODINIT_FUNC PyInit_omnistream(void) {
    PyObject *m;

    if (PyType_Ready(&StreamerType) < 0)
        return NULL;

    m = PyModule_Create(&omnistream_module);
    if (!m)
        return NULL;

    Py_INCREF(&StreamerType);
    if (PyModule_AddObject(m, "Streamer", (PyObject *)&StreamerType) < 0) {
        Py_DECREF(&StreamerType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}
