##############################################################
# Convenience functions for SAR image batch processing with ESA SNAP
# John Truckenbrodt, 2016-2017
##############################################################
import os

import pyroSAR
from pyroSAR import spatial
from .auxil import parse_recipe, parse_suffix, write_recipe, parse_node, insert_node, gpt


def geocode(infile, outdir, t_srs=None, tr=20, polarizations='all', shapefile=None, scaling='dB',
            geocoding_type='Range-Doppler', removeS1BoderNoise=True, offset=None, externalDEMFile=None, externalDEMNoDataValue=None, externalDEMApplyEGM=True, test=False):
    """
    wrapper function for geocoding SAR images using ESA SNAP

    infile: a pyroSAR.ID object or a file/folder name of a SAR scene
    outdir: the directory to write the final files to
    t_srs: a target geographic reference system (a string in WKT or PROJ4 format or a EPSG identifier number)
    tr: the target resolution in meters
    polarizations: the polarizations to be processed; can be a string for a single polarization e.g. 'VV' or a list of several polarizations e.g. ['VV', 'VH']
    shapefile: a vector file for subsetting the SAR scene to a test site
    scaling: should the output be in linear of decibel scaling? Input can be single strings e.g. 'dB' or a list of both: ['linear', 'dB']
    geocoding_type: the type of geocoding applied; can be either 'Range-Doppler' or 'SAR simulation cross correlation'
    removeS1BoderNoise: enables removal of S1 GRD border noise
    offset: a tuple defining offsets for left, right, top and bottom in pixels, e.g. (100, 100, 0, 0); this variable is overridden if a shapefile is defined
    externalDEMFile: the absolute path to an external DEM file
    externalDEMNoDataValue: the no data value of the external DEM. If not specified the function will try to read it from the specified external DEM.
    externalDEMApplyEGM: apply Earth Gravitational Model to external DEM?
    test: if set to True the workflow xml file is only written and not executed

    If only one polarization is selected the results are directly written to GeoTiff.
    Otherwise the results are first written to a folder containing ENVI files and then transformed to GeoTiff files (one for each polarization)
    If GeoTiff would directly be selected as output format for multiple polarizations then a multilayer GeoTiff
    is written by SNAP which is considered an unfavorable format
    """

    id = infile if isinstance(infile, pyroSAR.ID) else pyroSAR.identify(infile)

    if id.is_processed(outdir):
        print('scene {} already processed'.format(id.outname_base()))
        return

    ############################################
    # general setup

    if id.sensor in ['ASAR', 'ERS1', 'ERS2']:
        formatName = 'ENVISAT'
    elif id.sensor in ['S1A', 'S1B']:
        formatName = 'SENTINEL-1'
    else:
        raise RuntimeError('sensor not supported (yet)')

    if polarizations == 'all':
        polarizations = id.polarizations
    else:
        polarizations = [x for x in polarizations if x in id.polarizations]

    format = 'GeoTiff-BigTIFF' if len(polarizations) == 1 else 'ENVI'

    ############################################
    # parse base workflow

    workflow = parse_recipe('geocode')

    ############################################
    # Read node configuration

    read = workflow.find('.//node[@id="Read"]')
    read.find('.//parameters/file').text = id.scene
    read.find('.//parameters/formatName').text = formatName
    ############################################
    # Remove-GRD-Border-Noise node configuration

    if id.sensor in ['S1A', 'S1B'] and removeS1BoderNoise:
        insert_node(workflow, 'Read', parse_node('Remove-GRD-Border-Noise'))
        bn = workflow.find('.//node[@id="Remove-GRD-Border-Noise"]')
        bn.find('.//parameters/selectedPolarisations').text = ','.join(polarizations)
    ############################################
    # orbit file application node configuration

    orbit_lookup = {'ENVISAT': 'DELFT Precise (ENVISAT, ERS1&2) (Auto Download)',
                    'SENTINEL-1': 'Sentinel Precise (Auto Download)'}
    orbitType = orbit_lookup[formatName]
    if formatName == 'ENVISAT' and id.acquisition_mode == 'WSM':
        orbitType = 'DORIS Precise VOR (ENVISAT) (Auto Download)'

    orb = workflow.find('.//node[@id="Apply-Orbit-File"]')
    orb.find('.//parameters/orbitType').text = orbitType
    ############################################
    # calibration node configuration

    cal = workflow.find('.//node[@id="Calibration"]')

    # todo: check whether the following works with Sentinel-1
    if len(polarizations) > 1:
        cal.find('.//parameters/selectedPolarisations').text = ','.join(polarizations)
        cal.find('.//parameters/sourceBands').text = ','.join(['Intensity_' + x for x in polarizations])
    else:
        cal.find('.//parameters/selectedPolarisations').text = ','.join(polarizations)
        cal.find('.//parameters/sourceBands').text = 'Intensity'
    ############################################
    # terrain flattening node configuration

    tf = workflow.find('.//node[@id="Terrain-Flattening"]')
    if id.sensor in ['ERS1', 'ERS2'] or (id.sensor == 'ASAR' and id.acquisition_mode != 'APP'):
        tf.find('.//parameters/sourceBands').text = 'Beta0'
    else:
        tf.find('.//parameters/sourceBands').text = ','.join(['Beta0_' + x for x in polarizations])
    ############################################
    # configuration of node sequence for specific geocoding approaches

    if geocoding_type == 'Range-Doppler':
        insert_node(workflow, 'Terrain-Flattening', parse_node('Terrain-Correction'))
        tc = workflow.find('.//node[@id="Terrain-Correction"]')
        tc.find('.//parameters/sourceBands').text = ','.join(['Gamma0_' + x for x in polarizations])
    elif geocoding_type == 'SAR simulation cross correlation':
        insert_node(workflow, 'Terrain-Flattening', parse_node('SAR-Simulation'))
        insert_node(workflow, 'SAR-Simulation', parse_node('Cross-Correlation'))
        insert_node(workflow, 'Cross-Correlation', parse_node('SARSim-Terrain-Correction'))
        tc = workflow.find('.//node[@id="SARSim-Terrain-Correction"]')

        sarsim = workflow.find('.//node[@id="SAR-Simulation"]')
        sarsim.find('.//parameters/sourceBands').text = ','.join(['Gamma0_' + x for x in polarizations])
    else:
        raise RuntimeError('geocode_type not recognized')
    tc.find('.//parameters/pixelSpacingInMeter').text = str(tr)
    if t_srs:
        t_srs = spatial.crsConvert(t_srs, 'wkt')
        tc.find('.//parameters/mapProjection').text = t_srs
    else:
        tc.find('.//parameters/mapProjection').text = \
            'GEOGCS["WGS84(DD)",' \
            'DATUM["WGS84",' \
            'SPHEROID["WGS84", 6378137.0, 298.257223563]],' \
            'PRIMEM["Greenwich", 0.0],' \
            'UNIT["degree", 0.017453292519943295],' \
            'AXIS["Geodetic longitude", EAST],' \
            'AXIS["Geodetic latitude", NORTH]]'
    ############################################
    # add node for conversion from linear to db scaling
    if scaling is 'dB':
        lin2db = parse_node('lin2db')
        sourceNode = 'Terrain-Correction' if geocoding_type == 'Range-Doppler' else 'SARSim-Terrain-Correction'
        insert_node(workflow, sourceNode, lin2db)

        lin2db = workflow.find('.//node[@id="LinearToFromdB"]')
        lin2db.find('.//parameters/sourceBands').text = ','.join(['Gamma0_' + x for x in polarizations])

    ############################################
    # add subset node and add bounding box coordinates of defined shapefile
    if shapefile:
        shp = shapefile if isinstance(shapefile, spatial.vector.Vector) else spatial.vector.Vector(shapefile)
        bounds = spatial.bbox(shp.extent, shp.wkt)
        bounds.reproject(id.projection)
        intersect = spatial.intersect(id.bbox(), bounds)
        if not intersect:
            raise RuntimeError('no bounding box intersection between shapefile and scene')
        wkt = bounds.convert2wkt()[0]

        subset = parse_node('Subset')
        insert_node(workflow, 'Read', subset)

        subset = workflow.find('.//node[@id="Subset"]')
        subset.find('.//parameters/region').text = ','.join(map(str, [0, 0, id.samples, id.lines]))
        subset.find('.//parameters/geoRegion').text = wkt
    ############################################
    # configure subset node for pixel offsets
    if offset and not shapefile:
        subset = parse_node('Subset')
        insert_node(workflow, 'Read', subset)

        # left, right, top and bottom offset in pixels
        l, r, t, b = offset

        subset = workflow.find('.//node[@id="Subset"]')
        subset.find('.//parameters/region').text = ','.join(map(str, [l, t, id.samples-l-r, id.lines-t-b]))
        subset.find('.//parameters/geoRegion').text = ''
    ############################################
    # parametrize write node

    # create a suffix for the output file to identify processing steps performed in the workflow
    suffix = parse_suffix(workflow)

    if format == 'ENVI':
        outname = os.path.join(outdir, id.outname_base() + '_' + suffix)
    else:
        outname = os.path.join(outdir, '{}_{}_{}'.format(id.outname_base(), polarizations[0], suffix))

    write = workflow.find('.//node[@id="Write"]')
    write.find('.//parameters/file').text = outname
    write.find('.//parameters/formatName').text = format
    ############################################
    ############################################
    # select DEM type

    if externalDEMFile is not None:
        if os.path.isfile(externalDEMFile):
            if externalDEMNoDataValue is None:
                dem = spatial.raster.Raster(externalDEMFile)
                if dem.nodata is not None:
                    externalDEMNoDataValue = dem.nodata
                else:
                    raise RuntimeError('Cannot read NoData value from DEM file. Please specify externalDEMNoDataValue')
        else:
            raise RuntimeError('specified externalDEMFile does not exist')
        demname = 'External DEM'
    else:
        # SRTM 1arcsec is only available between -58 and +60 degrees. If the scene exceeds those latitudes SRTM 3arcsec is selected.
        if id.getCorners()['ymax'] > 60 or id.getCorners()['ymin'] < -58:
            demname = 'SRTM 3Sec'
        else:
            demname = 'SRTM 1Sec HGT'
        externalDEMFile = None
        externalDEMNoDataValue = 0

    for demName in workflow.findall('.//parameters/demName'):
        demName.text = demname
    for externalDEM in workflow.findall('.//parameters/externalDEMFile'):
        externalDEM.text = externalDEMFile
    for demNodata in workflow.findall('.//parameters/externalDEMNoDataValue'):
        demNodata.text = str(externalDEMNoDataValue)
    for egm in workflow.findall('.//parameters/externalDEMApplyEGM'):
        egm.text = str(externalDEMApplyEGM).lower()
    ############################################
    ############################################
    # write workflow to file
    write_recipe(workflow, outname + '_proc')

    # execute the newly written workflow
    if not test:
        try:
            gpt(outname + '_proc.xml')
        except RuntimeError:
            os.remove(outname + '_proc.xml')
