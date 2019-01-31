"""
Class to interpolate a band structure using BoltzTraP1
"""

__author__ = "Alex Ganose, Francesco Ricci and Alireza Faghaninia"
__copyright__ = "Copyright 2019, HackingMaterials"
__maintainer__ = "Alex Ganose"

import multiprocessing
import os

from collections import namedtuple
from functools import partial
from itertools import starmap

import numpy as np

from amset.interpolate.base import AbstractInterpolater
from amset.utils.constants import Ry_to_eV, hbar, A_to_m, m_to_cm, m_e, e
from amset.utils.general import outer
from pymatgen.electronic_structure.boltztrap import BoltztrapRunner

BoltzTraP1Parameters = namedtuple(
    'BoltzTraP1Parameters',
    ['num_symmetries', 'coefficients', 'min_band', 'max_band', 'allowed_ibands',
     'num_star_vectors', 'star_vectors', 'star_vector_products',
     'star_vector_products_sq'])

# defined globally to allow parallelisation to make use of shared memory,
# otherwise the parameters object (very large) has to be copied to each process
_parameters = {}


class BoltzTraP1Interpolater(AbstractInterpolater):
    """Class to interpolate band structures based on BoltzTraP1.

    The fitting algorithm is the Shankland-Koelling-Wood Fourier interpolation
    scheme, implemented in the BolzTraP1 software package.

    Details of the interpolation method are available in:

    1. R.N. Euwema, D.J. Stukel, T.C. Collins, J.S. DeWitt, D.G. Shankland,
       Phys. Rev. 178 (1969)  1419–1423.
    2. D.D. Koelling, J.H. Wood, J. Comput. Phys. 67 (1986) 253–262.
    3. Madsen, G. K. & Singh, D. J. Computer Physics Communications 175, 67–71
       (2006).

    The coefficient for fitting are calculated in BoltzTraP1, not in this code.
    The coefficients are used by this class to calculate the band eigenvalues.

    Note: This interpolater requires a modified version of BoltzTraP1 to run
    correctly. The modified version outputs the band structure coefficients to
    a file called "fort.123". A patch to modify BoltzTraP1 is provided in the
    "patch_for_boltztrap" directory.

    Args:
        band_structure (BandStructure): A pymatgen band structure object.
        num_electrons (num_electrons): The number of electrons in the system.
        coeff_file (str, optional): Path to a band structure coefficients file
            generated by a modified version of BoltzTraP1. If ``None``,
            BoltzTraP1 will be run to generate the file. Note, this requires
            a patched version of BoltzTraP1. More information can be found in
            the "patch_for_boltztrap" directory.
        max_temperature (int, optional): The maximum temperature at which to
            run BoltzTraP1 (if required). This will be used by BoltzTraP1 to
            decide how many bands to interpolate.
    """

    def __init__(self, band_structure, num_electrons, coeff_file=None,
                 max_temperature=None, n_jobs=-1,
                 **kwargs):
        super(BoltzTraP1Interpolater, self).__init__(
            band_structure, num_electrons, **kwargs)
        self._coeff_file = coeff_file
        self._max_temperature = max_temperature
        self._n_jobs = multiprocessing.cpu_count() if n_jobs == -1 else n_jobs
        self._lattice_matrix = band_structure.structure.lattice.matrix
        self._parameters_id = None

    def initialize(self):
        """Initialise the interpolater.

        This function will attempt to load the band structure coefficients.
        If a coefficient file is not provided, BoltzTraP1 will be run to
        generate it. This requires a modified version of BoltzTraP1 to run
        correctly. The modified version outputs the band structure coefficients
        to a file called "fort.123". A patch to modify BoltzTraP1 is provided in
        the "patch_for_boltztrap" directory.
        """
        if not self._coeff_file:
            self.logger.info('No coefficient file specified, running BoltzTraP '
                             'to generate it.')
            self._coeff_file = self._generate_coeff_file()

        self._parameters_id = id(self)

        # store the parameters in global scope so all subprocesses can access
        # it without having to copy the data.
        # the parameters are stored in a dictionary in case multiple
        # BoltzTraP1Interpolaters are running simultaneously
        global _parameters
        _parameters[self._parameters_id] = self._get_interpolation_parameters(
            self._coeff_file)

    def get_energy(self, kpoint, iband, return_velocity=False,
                   return_effective_mass=False):
        """Gets the interpolated energy for a specific k-point and band.

        Args:
            kpoint (np.ndarray): The k-point fractional coordinates.
            iband (int): The band index (1-indexed).
            return_velocity (bool, optional): Whether to return the band
                velocity.
            return_effective_mass (bool, optional): Whether to return the band
                effective mass.

        Returns:
            Union[int, tuple[int or np.ndarray]]: The band energies as a
            numpy array. If ``return_velocity`` or ``return_effective_mass`` are
            ``True`` a tuple is returned, formatted as::

                (energy, Optional[velocity], Optional[effecitve_mass])

            The velocity and effective mass are given as the 1x3 trace and
            full 3x3 tensor, respectively (along cartesian directions).
        """
        to_return = _get_energy_wrapper(
            self._get_interpolation_coefficients(iband),
            self._parameters_id, self._lattice_matrix,
            kpoint, return_velocity=return_velocity,
            return_effective_mass=return_effective_mass)

        if len(to_return) == 1:
            # if only energy just return that, otherwise return tuple
            return to_return[0]
        else:
            return tuple(to_return)

    def get_energies(self, kpoints, iband, scissor=0.0, is_cb=None,
                     return_velocity=False,
                     return_effective_mass=False):
        """Gets the interpolated energies for multiple k-points in a band.

        Args:
            kpoints (np.ndarray): The k-points in fractional coordinates.
            iband (int): The band index (1-indexed).
            scissor (float, optional): The amount by which the band gap is
                scissored.
            is_cb (bool, optional): Whether the band of interest is a conduction
                band. Ignored if ``scissor == 0``.
            return_velocity (bool, optional): Whether to return the band
                velocities.
            return_effective_mass (bool, optional): Whether to return the band
                effective masses.

        Returns:
            Union[np.ndarray, tuple[np.ndarray]]: The band energies as a
            numpy array. If ``return_velocity`` or ``return_effective_mass`` are
            ``True`` a tuple is returned, formatted as::

                (energies, Optional[velocities], Optional[effective_masses])

            The velocities and effective masses are given as the 1x3 trace and
            full 3x3 tensor, respectively (along cartesian directions).
        """
        self.logger.debug("Interpolating bands from coefficient file")
        self.logger.debug("band_indices: {}".format(iband))

        if scissor != 0.0 and is_cb is None:
            raise ValueError('To apply scissor set is_cb.')
        else:
            # shift will be zero if scissor is 0
            shift = (-1 if is_cb else 1) * scissor / 2

        coefficients = self._get_interpolation_coefficients(iband)
        fun = partial(_get_energy_wrapper, coefficients,
                      self._parameters_id, self._lattice_matrix,
                      return_velocity=True,
                      return_effective_mass=return_effective_mass)

        if self._n_jobs == 1:
            results = list(map(fun, kpoints))
        else:
            with multiprocessing.Pool(self._n_jobs) as p:
                results = p.map(fun, kpoints)

        to_return = [[r[0] - shift for r in results]]

        if return_velocity:
            to_return.append([r[1] for r in results])

        if return_effective_mass:
            to_return.append([r[2] for r in results])

        if len(to_return) == 1:
            # if only energy just return that, otherwise return tuple
            return to_return[0]
        else:
            return tuple(to_return)

    @property
    def parameters(self):
        """Get the BoltzTraP1Parameters."""
        if not self._parameters_id:
            self.initialize()

        return _parameters[self._parameters_id]

    def _generate_coeff_file(self):
        """Generate the band structure coefficients needed for interpolation.

        Coefficients are generated using BolzTraP1. This requires a modified
        version of BoltzTraP1 to run correctly. The modified version outputs the
        coefficients to a file called "fort.123". A patch to modify BoltzTraP1
        is provided in the "patch_for_boltztrap" directory.

        Returns:
            (str): A path to the coefficients file.

        Raises:
            RuntimeError: If the coefficient file could not be generated
                successfully.
        """
        # do NOT set scissor in runner
        btr = BoltztrapRunner(
            bs=self._band_structure, nelec=self._num_electrons,
            run_type='BANDS', doping=[1e20], tgrid=300,
            tmax=max([self._max_temperature, 300]))
        dirpath = btr.run(path_dir=self._calc_dir)
        coeff_file = os.path.join(dirpath, 'fort.123')

        if not os.path.exists(coeff_file):
            self.log_raise(RuntimeError,
                           'Coefficient file was not generated properly. '
                           'This requires a modified version of BoltzTraP. '
                           'See the patch_for_boltztrap" folder for more '
                           'information')
        else:
            self.logger.info('Finished generating coefficient file. Set '
                             'coeff_file variable to {} to skip this in the'
                             'future'.format(coeff_file))

        return coeff_file

    @staticmethod
    def _get_interpolation_parameters(coeff_file):
        """Extracts the interpolation parameters from the coeffient file.

        The coeffients should have been generated using a modified version of
        BoltzTraP1. More information on how to modify BoltzTrap1 to produce this
        file can be found in the "patch_for_boltztrap" directory.

        Args:
            coeff_file (str): Path to a band structure coefficients file
                generated by a modified version of BoltzTraP1.

        Returns:
            (BoltzTraP1Parameters): The interpolation parameters as a
            ``BoltzTraP1Parameters`` ``namedtuple``  object with the variables:

            - ``coefficients`` (np.ndarray): The band structure coefficients.
            - ``min_band`` (int): The index of the first band for which
              coefficients have been calculated (determined by the settings used
              to generate the coefficients file). Note, the bands are 1 indexed.
            - ``max_band`` (int): The index of the last band for which
              coefficients have been calculated (determined by the settings used
              to generate the coefficients file). Note, the bands are 1 indexed.
            - ``allowed_ibands``: The set of bands for which coefficients exist.
              Determined by the settings used to generate the coefficients file.
              Note, the bands are 1 indexed.
            - ``num_star_vectors`` (np.ndarray): The number of vectors in the
              star function for each G vector.
            - ``star_vectors`` (np.ndarray): The star functions for each G
              vector and symmetry.
            - ``star_vector_products`` (np.ndarray): The dot product of the star
              vectors with the cell matrix for each G vector and symmetry.
              Needed only to compute the first derivatives of energy.
            - ``star_vector_products_sq`` The square of the star vector products
              needed to calculate the second derivatives of energy.
        """
        with open(coeff_file) as f:
            _, num_g_vectors, num_symmetries, _ = f.readline().split()
            num_g_vectors = int(num_g_vectors)
            num_symmetries = int(num_symmetries)

            cell_matrix = np.fromstring(f.readline(), sep=' ', count=3 * 3
                                        ).reshape((3, 3)).astype('float')
            sym_ops = np.transpose(
                np.fromstring(f.readline(), sep=' ', count=3 * 3 * 192
                              ).reshape((192, 3, 3)).astype('int'),
                axes=(0, 2, 1))

            g_vectors = np.zeros((num_g_vectors, 3))

            coefficients = []
            iband = []
            min_band = 0
            max_band = 0
            for i, l in enumerate(f):
                if i < num_g_vectors:
                    g_vectors[i] = np.fromstring(l, sep=' ')
                elif i == num_g_vectors:
                    min_band, max_band = np.fromstring(l, sep=' ', dtype=int)
                    iband = [num_g_vectors + (b - min_band + 1)
                             for b in range(min_band, max_band + 1)]
                elif i in iband:
                    coefficients.append(np.fromstring(l, sep=' '))
                    iband.pop(iband.index(i))
                    if len(iband) == 0:
                        break

        allowed_ibands = set(range(min_band, max_band + 1))

        def calculate_star_function(g_vector):
            trial = sym_ops[:num_symmetries].dot(g_vector)
            stg = np.unique(trial.view(
                np.dtype(
                    (np.void, trial.dtype.itemsize * trial.shape[1])))).view(
                trial.dtype).reshape(-1, trial.shape[1])
            nst = len(stg)
            stg = np.concatenate((stg, np.zeros((num_symmetries - nst, 3))))
            return nst, stg

        num_star_vectors = np.zeros(num_g_vectors, dtype='int')
        star_vectors = np.zeros((num_g_vectors, num_symmetries, 3), order='F')
        star_vector_products = np.zeros((num_g_vectors, num_symmetries, 3),
                                        order='F')

        for nw in range(num_g_vectors):
            num_star_vectors[nw], star_vectors[nw] = calculate_star_function(
                g_vectors[nw])
            star_vector_products[nw] = star_vectors[nw].dot(cell_matrix)

        star_vector_products_sq = np.zeros((num_g_vectors,
                                            max(num_star_vectors), 3, 3),
                                           order='F')

        for nw in range(num_g_vectors):
            for i in range(num_star_vectors[nw]):
                star_vector_products_sq[nw, i] = outer(
                    star_vector_products[nw, i], star_vector_products[nw, i])

        return BoltzTraP1Parameters(
            num_symmetries=num_symmetries, coefficients=np.array(coefficients),
            min_band=min_band, max_band=max_band, allowed_ibands=allowed_ibands,
            num_star_vectors=num_star_vectors, star_vectors=star_vectors,
            star_vector_products=star_vector_products,
            star_vector_products_sq=star_vector_products_sq)

    def _get_interpolation_coefficients(self, iband=None):
        """Get the interpolation coefficients for a band.

        Args:
            iband (int, list[int], optional): A band index or list of band
                indicies for which to get the interpolation coefficients.
                Band indicies are 1 indexed. If ``None``, the coefficients for
                all available bands will be returned.

        Returns:
            (np.ndarray): The band structure coefficients.

        Raises:
            ValueError: If iband contains an index for which the coefficients
                have note been calculated.
        """
        if isinstance(iband, int):
            iband = [iband]

        if not iband:
            iband = sorted(self.parameters.allowed_ibands)

        if not set(iband).issubset(self.parameters.allowed_ibands):
            raise ValueError("At least one band is not in range : {}-{}. "
                             "Try increasing max_Ecut to include more "
                             "bands.".format(self.parameters.min_band,
                                             self.parameters.max_band))

        # normalise the bands to minimum band
        iband = [b - self.parameters.min_band for b in iband]

        if len(iband) == 1:
            return self.parameters.coefficients[iband][0]
        else:
            return self.parameters.coefficients[iband]


def _get_energy_wrapper(coefficients, parameters_id, matrix, kpoint,
                        return_velocity=False, return_effective_mass=False):
    """Wrapper for parallelising the integration energy calculation.

    Args:
        coefficients (np.ndarray): The integration coefficients.
        parameters_id (str): Key for global parameters dictionary. Used to
            share large parameters data in memory across multiple processes.
        kpoint (np.ndarray): The k-point fractional coordinates.
        matrix (np.ndarray): 3x3 array of the direct lattice matrix
            used to convert the velocity from fractional to cartesian
            coordinates.
        return_velocity (bool, optional): Whether to return the band
            velocity.
        return_effective_mass (bool, optional): Whether to return the band
            effective mass.

    Returns:
        (tuple[int or np.ndarray]): A tuple containing the band energy, and
        optionally the velocity and effective mass if asked for. The
        velocities and effective masses are given as the 1x3 trace and
        full 3x3 tensor, respectively (along cartesian directions).
    """
    parameters = _parameters[parameters_id]

    arg = 2 * np.pi * parameters.star_vectors.dot(kpoint)
    cos_arg = np.cos(arg)
    spwre = (np.sum(cos_arg, axis=1) - (parameters.num_symmetries -
                                        parameters.num_star_vectors)
             ) / parameters.num_star_vectors

    energy = spwre.dot(coefficients)
    to_return = [energy * Ry_to_eV]

    if return_velocity:
        sin_arg = np.sin(arg)[:, :, np.newaxis]
        dspwre = np.sum(parameters.star_vector_products * sin_arg, axis=1
                        ) / parameters.num_star_vectors[:, np.newaxis]
        d_energy = np.dot(dspwre.T, coefficients)
        matrix_norm = matrix / np.linalg.norm(matrix)
        to_return.append(abs(np.dot(matrix_norm, d_energy)) *
                         A_to_m * m_to_cm * Ry_to_eV / (hbar * 0.52917721067))

    if return_effective_mass:
        ddspwre = np.sum(
            parameters.star_vector_products_sq *
            -cos_arg[:, :, np.newaxis, np.newaxis], axis=1
        ) / parameters.num_star_vectors[:, np.newaxis, np.newaxis]
        dd_energy = np.dot(ddspwre.T, coefficients)
        to_return.append(e * 0.52917721067 ** 2 * hbar ** 2 /
                         (dd_energy * m_e * A_to_m ** 2 * Ry_to_eV))

    return to_return
