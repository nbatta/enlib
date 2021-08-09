# Wavelet transforms. The main part is defining the wavelet filters. I want something that can
# 1. tell me the lmax for each scale, so I can decide on the resolution that scale needs
# 2. evaluate a basis function given a set of ls. Might be easier to implement if all bases
#    are evaluated at the same time...
#
# Could be easiest to implement by evaluating them as 1d functions of l internally, and then
# interpolate those functions onto the requested ls later. But that can be an implementation
# detail.
import numpy as np
from pixell import enmap, utils, wcsutils, curvedsky, sharp
from . import multimap

class WaveletTransform:
	"""This class implements a wavelet tansform. It provides thw forwards and
	backwards wavelet transforms map2wave and wave2map, where map is a normal enmap
	and the wavelet coefficients are represented as multimaps."""
	def __init__(self, uht, basis=ButterTrim()):
		"""Initialize the WaveletTransform. Arguments:
		* uht: An inscance of uharm.UHT, which specifies how to do harmonic transforms
		  (flat-sky vs. curved sky and what lmax).
		* basis: A basis-generating function, which provides the definition of the wavelet
		  filters. Defaults to ButterTrim(), which is fast to evaluate and decently local
		  both spatially and harmonically.

		Flat-sky transforms should be exact. Curved-sky transforms become slightly inaccurate
		on small patches.

		Currently the curved-sky case uses wavelet maps with twice the naively needed resolution
		to make up for the deficiency of CAR quadrature. In the future better CAR quadrature will
		be available, but it would also be possible to use gauss-legendre pixelization internally."""
		self.uht   = uht
		self.basis = basis
		ires       = np.max(enmap.pixshapebounds(uht.shape, uht.wcs))
		# Respect the lmin and lmax in the basis if they are present, but otherwise
		# determine them ourselves.
		if self.basis.lmax is None or self.basis.lmin is None:
			lmin = self.basis.lmin; lmax = self.basis.lmax
			if lmax is None: lmax = min(int(np.ceil(np.pi/ires)),uht.lmax)
			if lmin is None: lmin = min(int(np.ceil(np.pi/np.max(enmap.extent(uht.shape, uht.wcs)))),lmax)
			self.basis = basis.with_bounds(lmin, lmax)
		# Build the geometries for each wavelet scale
		if uht.mode == "flat":
			oress      = np.maximum(np.pi/self.basis.lmaxs, ires)
			self.geometries = [make_wavelet_geometry_flat(uht.shape, uht.wcs, ires, ores) for ores in oress[:-1]] + [(uht.shape, uht.wcs)]
			# Evaluating the filters like this instead of using modlmap separately per geometry ensures that
			# no rounding errors sneak in.
			self.filters = [self.basis(i, enmap.resample_fft(uht.l, geo[0], norm=None, corner=True)) for i, geo in enumerate(self.geometries)]
		else:
			# Our quadrature requires twice the ideal resolution for now.
			# May be solved with ducc0 in the future.
			oress        = np.maximum(np.pi/self.basis.lmaxs/2, ires)
			self.geometries = [make_wavelet_geometry_curved(uht.shape, uht.wcs, ores) for ores in oress]
			self.filters = [self.basis(i, uht.l) for i, geo in enumerate(self.geometries)]
	@property
	def shape(self): return self.uht.shape
	@property
	def wcs(self): return self.uht.shape
	@property
	def geometry(self): return self.shape, self.wcs
	def map2wave(self, map, owave=None):
		"""Transform from an enmap map[...,ny,nx] to a multimap of wavelet coefficients,
		which is effectively a group of enmaps with the same pre-dimensions but varying shape.
		If owave is provided, it should be a multimap with the right shape (compatible with
		the .geometries member of this class), and will be overwritten with the result. In
		any case the resulting wavelet coefficients are returned."""
		# Output geometry. Can't just use our existing one because it doesn't know about the
		# map pre-dimensions. There should be an easier way to do this.
		geos = [(map.shape[:-2]+tuple(shape[-2:]), wcs) for (shape, wcs) in self.geometries]
		if owave is None: owave = multimap.zeros(geos, map.dtype)
		if self.uht.mode == "flat":
			fmap = enmap.fft(map, normalize=False)/map.npix
			for i, (shape, wcs) in enumerate(self.geometries):
				fsmall  = enmap.resample_fft(fmap, shape, norm=None, corner=True)
				fsmall *= self.filters[i]
				owave.map[i] = enmap.ifft(fsmall, normalize=False).real
		else:
			ainfo = sharp.alm_info(lmax=self.basis.lmax)
			alm   = curvedsky.map2alm(map, ainfo=ainfo)
			for i, (shape, wcs) in enumerate(self.geometries):
				smallinfo = sharp.alm_info(lmax=self.basis.lmaxs[i])
				asmall    = sharp.transfer_alm(ainfo, alm, smallinfo)
				smallinfo.lmul(asmall, self.filters[i], asmall)
				curvedsky.alm2map(asmall, owave.map[i])
		return owave
	def wave2map(self, wave, omap=None):
		"""Transform from the wavelet coefficients wave (multimap), to the corresponding enmap.
		If omap is provided, it must have the correct geometry (the .geometry member of this class),
		and will be overwritten with the result. In any case the result is returned."""
		if self.uht.mode == "flat":
			# Hard to save memory by specifying omap in this case
			fomap = enmap.zeros(wave.pre + self.uht.shape[-2:], self.uht.wcs, np.result_type(wave.dtype,0j))
			for i, (shape, wcs) in enumerate(self.geometries):
				fsmall  = enmap.fft(wave.map[i], normalize=False)
				fsmall /= fsmall.npix
				enmap.resample_fft(fsmall, self.uht.shape, fomap=fomap, norm=None, corner=True, op=np.add)
			tmp = enmap.ifft(fomap, normalize=False).real
			if omap is None: omap    = tmp
			else:            omap[:] = tmp
			return omap
		else:
			ainfo = sharp.alm_info(lmax=self.basis.lmax)
			oalm  = np.zeros(wave.pre + (ainfo.nelem,), dtype=np.result_type(wave.dtype,0j))
			for i, (shape, wcs) in enumerate(self.geometries):
				smallinfo = sharp.alm_info(lmax=self.basis.lmaxs[i])
				asmall    = curvedsky.map2alm(wave.map[i], ainfo=smallinfo)
				sharp.transfer_alm(smallinfo, asmall, ainfo, oalm, op=np.add)
			if omap is None:
				omap = enmap.zeros(wave.pre + self.uht.shape[-2:], self.uht.wcs, wave.dtype)
			return curvedsky.alm2map(oalm, omap)

######## Wavelet basis generators ########

class Butterworth:
	"""Butterworth waveleth basis. Built from differences between Butterworth lowpass filters,
	which have a good tradeoff between harmonic and spatial localization. However it doesn't
	have the sharp boundaries in harmonic space that needlets or scale-discrete wavelets do.
	This is a problem when we want to reduce the resolution of the wavelet maps. With a discrete
	cutoff this can be done losslessly, but with these Butterworth wavelets there's always some
	tail of the basis that extneds to arbitrarily high l, making resolution reduction lossy.
	This loss is controlled with the tol parameter."""
	# 1+2**a = 1/q => a = log2(1/tol-1)
	def __init__(self, step=2, shape=7, tol=1e-3, lmin=None, lmax=None):
		self.step = step; self.shape = shape; self.tol = tol
		self.lmin = lmin; self.lmax  = lmax
		if lmax is not None:
			if lmin is None: lmin = 1
			self._finalize()
	def with_bounds(self, lmin, lmax):
		"""Return a new instance with the given multipole bounds"""
		return Butterworth(step=self.step, shape=self.shape, tol=self.tol, lmin=lmin, lmax=lmax)
	def __call__(self, i, l):
		if i == self.n-1: profile  = np.full(l.shape, 1.0)
		else:             profile  = self.kernel(i,   l)
		if i > 0:         profile -= self.kernel(i-1, l)
		return profile
	def kernel(self, i, l):
		return 1/(1 + (l/(self.lmin*self.step**(i+0.5)))**(self.shape/np.log(self.step)))
	def _finalize(self):
		self.n        = int((np.log(self.lmax)-np.log(self.lmin))/np.log(self.step))
		# 1+(l/(lmin*(step**(i+0.5))))**a = 1/tol =>
		# l = (1/tol-1)**(1/a) * lmin*(step**(i+0.5))
		self.lmaxs    = np.round(self.lmin * (1/self.tol-1)**(np.log(self.step)/self.shape) * self.step**(np.arange(self.n)+0.5)).astype(int)
		self.lmaxs[-1] = self.lmax

class ButterTrim:
	"""Butterworth waveleth basis made harmonically compact by clipping off the tails.
	Built from differences between trimmed Butterworth lowpass filters. This trimming
	sacrifices some signal suppression at high radius, but this is a pretty small effect
	even with quite aggressive trimming."""
	def __init__(self, step=2, shape=7, trim=1e-2, lmin=None, lmax=None):
		self.step = step; self.shape = shape; self.trim = trim
		self.lmin = lmin; self.lmax  = lmax
		if lmax is not None:
			if lmin is None: lmin = 1
			self._finalize()
	def with_bounds(self, lmin, lmax):
		"""Return a new instance with the given multipole bounds"""
		return ButterTrim(step=self.step, shape=self.shape, trim=self.trim, lmin=lmin, lmax=lmax)
	def __call__(self, i, l):
		if i == self.n-1: profile  = np.full(l.shape, 1.0)
		else:             profile  = self.kernel(i,   l)
		if i > 0:         profile -= self.kernel(i-1, l)
		return profile
	def kernel(self, i, l):
		return trim_kernel(1/(1 + (l/(self.lmin*self.step**(i+0.5)))**(self.shape/np.log(self.step))), self.trim)
	def _finalize(self):
		self.n        = int((np.log(self.lmax)-np.log(self.lmin))/np.log(self.step))
		# 1/(1+(l/(lmin*(step**(i+0.5))))**a)*(1+2*trim)-trim = 0
		# => l = ((1+2*trim)/trim-1)**(1/a) * (lmin*(step**(i+0.5)))
		self.lmaxs    = np.ceil(self.lmin * ((1+2*self.trim)/self.trim-1)**(np.log(self.step)/self.shape) * self.step**(np.arange(self.n)+0.5)).astype(int)
		self.lmaxs[-1] = self.lmax

class AdriSD:
	"""Scale-discrete wavelet basis provided by Adri's optweight library.
	A bit heavy to initialize."""
	def __init__(self, lamb=2, lmin=None, lmax=None):
		self.lamb = lamb; self.lmin = lmin; self.lmax = lmax
		if lmax is not None:
			if lmin is None: lmin = 1
			self._finalize()
	def with_bounds(self, lmin, lmax):
		"""Return a new instance with the given multipole bounds"""
		return AdriSD(lamb=self.lamb, lmin=lmin, lmax=lmax)
	@property
	def n(self): return len(self.profiles)
	def __call__(self, i, l):
		return np.interp(l, np.arange(self.profiles[i].size), self.profiles[i])
	def _finalize(self):
		from optweight import wlm_utils
		self.profiles, self.lmaxs = wlm_utils.get_sd_kernels(self.lamb, self.lmax, lmin=self.lmin)
		self.profiles **= 2

####### Helper functions #######

def trim_kernel(a, tol): return np.clip(a*(1+2*tol)-tol,0,1)

def make_wavelet_geometry_flat(ishape, iwcs, ires, ores):
	# I've found that, possibly due to rounding or imprecise scaling, I sometimes need to add up
	# to +2 to avoid parts of some basis functions being cut off. I add +5 to get some margin -
	# it's cheap anyway - though it would be best to get to the bottom of it.
	oshape    = (np.ceil(np.array(ishape[-2:])*ires/ores)).astype(int)+5
	oshape    = np.minimum(oshape, ishape[-2:])
	owcs      = wcsutils.scale(iwcs, oshape[-2:]/ishape[-2:], rowmajor=True, corner=True)
	return oshape, owcs

def make_wavelet_geometry_curved(ishape, iwcs, ores, pad=0):
	# NOTE: This function assumes:
	# * cylindrical coordinates
	# * dec increases with y, ra decreases with x
	# The latter can be generalized with a fewe more checks.
	# We need to be able to perform SHTs on these, so we can't just generate an arbitrary
	# pixelization. Find the fullsky geometry with the desired resolution, and cut out the
	# part best matching our patch.
	res = np.pi/np.ceil(np.pi/ores)
	# Find the bounding box of our patch, and make sure it's in bounds.
	box = enmap.corners(ishape, iwcs)
	box[:,0] = np.clip(box[:,0], -np.pi/2, np.pi/2)
	box[1,1] = box[0,1] + np.clip(box[1,1]-box[0,1],-2*np.pi,2*np.pi)
	# Build a full-sky geometry for which we have access to quadrature
	tgeo = enmap.Geometry(*enmap.fullsky_geometry(res=res))
	# Figure out how we need to crop this geometry to match our target patch
	pbox = enmap.skybox2pixbox(*tgeo, box)
	pbox[np.argmax(pbox[:,0]),0] += 1 # Make sure we include the final full-sky row
	pbox[:,1] += utils.rewind(pbox[0,1], period=tgeo.shape[1])-pbox[0,1]
	# Round to whole pixels and slice the geometry
	pbox = utils.nint(pbox)
	# Pad the pixbox with extra pixels if requested
	oshape, owcs = tgeo.submap(pixbox=pbox)
	return oshape, owcs
