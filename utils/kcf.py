import numpy as np
import cv2
from . import fhog


def fftd(img, backwards=False):
    return cv2.dft(np.float32(img), flags=((cv2.DFT_INVERSE | cv2.DFT_SCALE) if backwards else cv2.DFT_COMPLEX_OUTPUT))


def real(img):
    return img[:, :, 0]


def imag(img):
    return img[:, :, 1]


def complexMultiplication(a, b):
    res = np.zeros(a.shape, a.dtype)

    res[:, :, 0] = a[:, :, 0] * b[:, :, 0] - a[:, :, 1] * b[:, :, 1]
    res[:, :, 1] = a[:, :, 0] * b[:, :, 1] + a[:, :, 1] * b[:, :, 0]
    return res


def complexDivision(a, b):
    res = np.zeros(a.shape, a.dtype)
    divisor = 1. / (b[:, :, 0] ** 2 + b[:, :, 1] ** 2)

    res[:, :, 0] = (a[:, :, 0] * b[:, :, 0] + a[:, :, 1] * b[:, :, 1]) * divisor
    res[:, :, 1] = (a[:, :, 1] * b[:, :, 0] + a[:, :, 0] * b[:, :, 1]) * divisor
    return res


def rearrange(img):
    assert (img.ndim == 2)
    img_ = np.zeros(img.shape, img.dtype)
    xh, yh = img.shape[1] // 2, img.shape[0] // 2
    img_[0:yh, 0:xh], img_[yh:img.shape[0], xh:img.shape[1]] = img[yh:img.shape[0], xh:img.shape[1]], img[0:yh, 0:xh]
    img_[0:yh, xh:img.shape[1]], img_[yh:img.shape[0], 0:xh] = img[yh:img.shape[0], 0:xh], img[0:yh, xh:img.shape[1]]
    return img_


def x2(rect):
    return rect[0] + rect[2]


def y2(rect):
    return rect[1] + rect[3]


def limit(rect, limit):
    if rect[0] + rect[2] > limit[0] + limit[2]:
        rect[2] = limit[0] + limit[2] - rect[0]
    if rect[1] + rect[3] > limit[1] + limit[3]:
        rect[3] = limit[1] + limit[3] - rect[1]
    if rect[0] < limit[0]:
        rect[2] -= (limit[0] - rect[0])
        rect[0] = limit[0]
    if rect[1] < limit[1]:
        rect[3] -= (limit[1] - rect[1])
        rect[1] = limit[1]
    if rect[2] < 0:
        rect[2] = 0
    if rect[3] < 0:
        rect[3] = 0
    return rect


def getBorder(original, limited):
    res = [0, 0, 0, 0]
    res[0] = limited[0] - original[0]
    res[1] = limited[1] - original[1]
    res[2] = x2(original) - x2(limited)
    res[3] = y2(original) - y2(limited)
    assert (np.all(np.array(res) >= 0))
    return res


def subwindow(img, window, borderType=cv2.BORDER_CONSTANT):
    cutWindow = [x for x in window]
    limit(cutWindow, [0, 0, img.shape[1], img.shape[0]])
    if (cutWindow[2] <= 0 or cutWindow[3] <= 0):
        return None

    border = getBorder(window, cutWindow)
    res = img[cutWindow[1]:cutWindow[1] + cutWindow[3], cutWindow[0]:cutWindow[0] + cutWindow[2]]

    if border != [0, 0, 0, 0]:
        res = cv2.copyMakeBorder(res, border[1], border[3], border[0], border[2], borderType)
    return res


class KCFTracker:
    def __init__(self, hog=True, fixed_window=True, multiscale=True):
        self.lambdar = 0.0001  # regularization
        self.padding = 2.5  # extra area surrounding the target
        self.output_sigma_factor = 0.125  # bandwidth of gaussian target

        if (hog):  # HOG feature
            # VOT
            self.interp_factor = 0.012  # linear interpolation factor for adaptation
            self.sigma = 0.6  # gaussian kernel bandwidth
            self.cell_size = 4  # HOG cell size
            self._hogfeatures = True
        else:  # raw gray-scale image # aka CSK tracker
            self.interp_factor = 0.075
            self.sigma = 0.2
            self.cell_size = 1
            self._hogfeatures = False

        if (multiscale):
            self.template_size = 96
            self.scale_step = 1.05
            self.scale_weight = 0.96
        elif (fixed_window):
            self.template_size = 96
            self.scale_step = 1
        else:
            self.template_size = 1
            self.scale_step = 1

        self._tmpl_sz = [0, 0]
        self._roi = [0., 0., 0., 0.]
        self.size_patch = [0, 0, 0]
        self._scale = 1.
        self._alphaf = None
        self._prob = None
        self._tmpl = None
        self.hann = None

    def subPixelPeak(self, left, center, right):
        divisor = 2 * center - right - left
        return 0 if abs(divisor) < 1e-3 else 0.5 * (right - left) / divisor

    def createHanningMats(self):
        hann2t, hann1t = np.ogrid[0:self.size_patch[0], 0:self.size_patch[1]]

        hann1t = 0.5 * (1 - np.cos(2 * np.pi * hann1t / (self.size_patch[1] - 1)))
        hann2t = 0.5 * (1 - np.cos(2 * np.pi * hann2t / (self.size_patch[0] - 1)))
        hann2d = hann2t * hann1t

        if self._hogfeatures:
            hann1d = hann2d.reshape(self.size_patch[0] * self.size_patch[1])
            self.hann = np.zeros((self.size_patch[2], 1), np.float32) + hann1d
        else:
            self.hann = hann2d
        self.hann = self.hann.astype(np.float32)

    def createGaussianPeak(self, sizey, sizex):
        syh, sxh = sizey / 2, sizex / 2
        output_sigma = np.sqrt(sizex * sizey) / self.padding * self.output_sigma_factor
        mult = -0.5 / (output_sigma * output_sigma)
        y, x = np.ogrid[0:sizey, 0:sizex]
        y, x = (y - syh) ** 2, (x - sxh) ** 2
        res = np.exp(mult * (y + x))
        return fftd(res)

    def gaussianCorrelation(self, x1, x2):
        if self._hogfeatures:
            c = np.zeros((self.size_patch[0], self.size_patch[1]), np.float32)
            for i in range(self.size_patch[2]):
                x1aux = x1[i, :].reshape((self.size_patch[0], self.size_patch[1]))
                x2aux = x2[i, :].reshape((self.size_patch[0], self.size_patch[1]))
                caux = cv2.mulSpectrums(fftd(x1aux), fftd(x2aux), 0, conjB=True)
                caux = real(fftd(caux, True))
                c += caux
            c = rearrange(c)
        else:
            c = cv2.mulSpectrums(fftd(x1), fftd(x2), 0, conjB=True)
            c = fftd(c, True)
            c = real(c)
            c = rearrange(c)

        if x1.ndim == 3 and x2.ndim == 3:
            d = (np.sum(x1[:, :, 0] * x1[:, :, 0]) + np.sum(x2[:, :, 0] * x2[:, :, 0]) - 2.0 * c) / (
                        self.size_patch[0] * self.size_patch[1] * self.size_patch[2])
        elif x1.ndim == 2 and x2.ndim == 2:
            d = (np.sum(x1 * x1) + np.sum(x2 * x2) - 2.0 * c) / (
                        self.size_patch[0] * self.size_patch[1] * self.size_patch[2])

        d = d * (d >= 0)
        d = np.exp(-d / (self.sigma * self.sigma))

        return d

    def getFeatures(self, image, inithann, scale_adjust=1.0):
        extracted_roi = [0, 0, 0, 0]
        cx = self._roi[0] + self._roi[2] / 2
        cy = self._roi[1] + self._roi[3] / 2

        if inithann:
            padded_w = self._roi[2] * self.padding
            padded_h = self._roi[3] * self.padding

            if self.template_size > 1:
                if padded_w >= padded_h:
                    self._scale = padded_w / float(self.template_size)
                else:
                    self._scale = padded_h / float(self.template_size)
                self._tmpl_sz[0] = padded_w // self._scale
                self._tmpl_sz[1] = padded_h // self._scale
            else:
                self._tmpl_sz[0] = int(padded_w)
                self._tmpl_sz[1] = int(padded_h)
                self._scale = 1.

            if self._hogfeatures:
                self._tmpl_sz[0] = int(
                    self._tmpl_sz[0] / (2 * self.cell_size)) * 2 * self.cell_size + 2 * self.cell_size
                self._tmpl_sz[1] = int(
                    self._tmpl_sz[1] / (2 * self.cell_size)) * 2 * self.cell_size + 2 * self.cell_size
            else:
                self._tmpl_sz[0] = int(self._tmpl_sz[0] / 2) * 2
                self._tmpl_sz[1] = int(self._tmpl_sz[1] / 2) * 2

        extracted_roi[2] = int(scale_adjust * self._scale * self._tmpl_sz[0])
        extracted_roi[3] = int(scale_adjust * self._scale * self._tmpl_sz[1])
        extracted_roi[0] = int(cx - extracted_roi[2] / 2)
        extracted_roi[1] = int(cy - extracted_roi[3] / 2)

        z = subwindow(image, extracted_roi, cv2.BORDER_REPLICATE)
        if isinstance(z, type(None)):
            raise Exception('invalid subwindow result')

        if z.shape[1] != self._tmpl_sz[0] or z.shape[0] != self._tmpl_sz[1]:
            z = cv2.resize(z, tuple(self._tmpl_sz))

        if self._hogfeatures:
            mapp = {'sizeX': 0, 'sizeY': 0, 'numFeatures': 0, 'map': 0}
            mapp = fhog.getFeatureMaps(z, self.cell_size, mapp)
            mapp = fhog.normalizeAndTruncate(mapp, 0.2)
            mapp = fhog.PCAFeatureMaps(mapp)
            self.size_patch = list(map(int, [mapp['sizeY'], mapp['sizeX'], mapp['numFeatures']]))
            FeaturesMap = mapp['map'].reshape((self.size_patch[0] * self.size_patch[1], self.size_patch[2])).T
        else:
            if z.ndim == 3 and z.shape[2] == 3:
                FeaturesMap = cv2.cvtColor(z, cv2.COLOR_BGR2GRAY)
            elif z.ndim == 2:
                FeaturesMap = z
            FeaturesMap = FeaturesMap.astype(np.float32) / 255.0 - 0.5
            self.size_patch = [z.shape[0], z.shape[1], 1]

        if inithann:
            self.createHanningMats()

        FeaturesMap = self.hann * FeaturesMap
        return FeaturesMap

    def detect(self, z, x):
        k = self.gaussianCorrelation(x, z)
        res = real(fftd(complexMultiplication(self._alphaf, fftd(k)), True))

        _, pv, _, pi = cv2.minMaxLoc(res)
        p = [float(pi[0]), float(pi[1])]

        if 0 < pi[0] < res.shape[1] - 1:
            p[0] += self.subPixelPeak(res[pi[1], pi[0] - 1], pv, res[pi[1], pi[0] + 1])
        if 0 < pi[1] < res.shape[0] - 1:
            p[1] += self.subPixelPeak(res[pi[1] - 1, pi[0]], pv, res[pi[1] + 1, pi[0]])

        p[0] -= res.shape[1] / 2.
        p[1] -= res.shape[0] / 2.

        return p, pv

    def train(self, x, train_interp_factor):
        k = self.gaussianCorrelation(x, x)
        alphaf = complexDivision(self._prob, fftd(k) + self.lambdar)

        self._tmpl = (1 - train_interp_factor) * self._tmpl + train_interp_factor * x
        self._alphaf = (1 - train_interp_factor) * self._alphaf + train_interp_factor * alphaf

    def init(self, image, roi):
        self._roi = list(map(float, roi))
        assert roi[2] > 0 and roi[3] > 0
        try:
            self._tmpl = self.getFeatures(image, 1)
            self._prob = self.createGaussianPeak(self.size_patch[0], self.size_patch[1])
            self._alphaf = np.zeros((self.size_patch[0], self.size_patch[1], 2), np.float32)
            self.train(self._tmpl, 1.0)

        except Exception as e:
            pass

    def update(self, image, track_threshold=0.4, reject_aspectRatio=2.0):
        if self._roi[0] + self._roi[2] <= 0:  self._roi[0] = -self._roi[2] + 1
        if self._roi[1] + self._roi[3] <= 0:  self._roi[1] = -self._roi[2] + 1
        if self._roi[0] >= image.shape[1] - 1:  self._roi[0] = image.shape[1] - 2
        if self._roi[1] >= image.shape[0] - 1:  self._roi[1] = image.shape[0] - 2

        cx = self._roi[0] + self._roi[2] / 2.
        cy = self._roi[1] + self._roi[3] / 2.

        try:
            loc, peak_value = self.detect(self._tmpl, self.getFeatures(image, 0, 1.0))

            if self.scale_step != 1:
                new_loc1, new_peak_value1 = self.detect(self._tmpl, self.getFeatures(image, 0, 1.0 / self.scale_step))
                new_loc2, new_peak_value2 = self.detect(self._tmpl, self.getFeatures(image, 0, self.scale_step))

                if self.scale_weight * new_peak_value1 > peak_value and new_peak_value1 > new_peak_value2:
                    loc = new_loc1
                    peak_value = new_peak_value1
                    self._scale /= self.scale_step
                    self._roi[2] /= self.scale_step
                    self._roi[3] /= self.scale_step
                elif self.scale_weight * new_peak_value2 > peak_value:
                    loc = new_loc2
                    peak_value = new_peak_value2
                    self._scale *= self.scale_step
                    self._roi[2] *= self.scale_step
                    self._roi[3] *= self.scale_step

            self._roi[0] = cx - self._roi[2] / 2.0 + loc[0] * self.cell_size * self._scale
            self._roi[1] = cy - self._roi[3] / 2.0 + loc[1] * self.cell_size * self._scale

            if self._roi[0] >= image.shape[1] - 1:  self._roi[0] = image.shape[1] - 1
            if self._roi[1] >= image.shape[0] - 1:  self._roi[1] = image.shape[0] - 1
            if self._roi[0] + self._roi[2] <= 0:  self._roi[0] = -self._roi[2] + 2
            if self._roi[1] + self._roi[3] <= 0:  self._roi[1] = -self._roi[3] + 2
            assert self._roi[2] > 0 and self._roi[3] > 0

            x = self.getFeatures(image, 0, 1.0)
            self.train(x, self.interp_factor)

            if peak_value < track_threshold or self._roi[3] / self._roi[2] > reject_aspectRatio or self._roi[2] / self._roi[3] > reject_aspectRatio:
                return False, list(map(int, self._roi))

            return True, list(map(int, self._roi))

        except Exception as e:
            return False, list(map(int, self._roi))
