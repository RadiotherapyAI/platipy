from .app import web_app
from loguru import logger
import tempfile
import pydicom
import os

from celery import Celery
from celery import current_app
from celery.bin import worker
from celery.task.control import revoke

from ..dicom.communication import DicomConnector

from .models import db, Dataset, DataObject


# Celery configuration
# TODO Should be in a configuration file
web_app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
web_app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/0'
celery = Celery(__name__, broker='redis://localhost:6379/0')
celery.conf.update(web_app.config)


@celery.task(bind=True)
def retrieve_task(task, data_object_id):
    """
    Fetch a Dicom Object from the dicom location using the retrieve_type GET or MOVE
    """

    do = DataObject.query.filter_by(id=data_object_id).first()

    dicom_connector = DicomConnector(host=do.dataset.from_dicom_location.host,
                                     port=do.dataset.from_dicom_location.port,
                                     ae_title=do.dataset.from_dicom_location.ae_title)
    dicom_verify = dicom_connector.verify()

    if not dicom_verify:
        logger.error('Unable to connect to Dicom Location: {0} {1} {2}'.format(
            do.dataset.from_dicom_location.host, do.dataset.from_dicom_location.port, do.dataset.from_dicom_location.ae_title))
        return

    dicom_path = dicom_connector.download_series(do.series_instance_uid)

    do.is_fetched = True
    do.path = dicom_path
    db.session.commit()


@celery.task(bind=True)
def move_task(task, endpoint, seriesUIDs, host, port, ae_title):
    """
    Background task that triggers the Dicom MOVE operation at the given endpoint
    for the given seriesUIDs
    """

    # For each series UID supplied, fetch the image series and run the algorithm
    total = len(seriesUIDs)
    count = 0

    dicom_connector = DicomConnector(host=host, port=port, ae_title=ae_title)

    task.update_state(state='PROGRESS',
                      meta={'current': count, 'total': total,
                            'status': 'Verifying dicom location'})

    dicom_verify = dicom_connector.verify()

    if dicom_verify == None:
        return {'current': 100, 'total': 100, 'status': 'Unable to connect to dicom location'}

    for suid in seriesUIDs:

        task.update_state(state='PROGRESS',
                          meta={'current': count, 'total': total,
                                'status': 'Moving series for UID: {0}'.format(suid)})

        logger.info('Moving Series with UID: {0}'.format(suid))
        dicom_connector.move_series(suid)

        count = count + 1

    task.update_state(state='SUCCESS',
                      meta={'current': total, 'total': total,
                            'status': 'Move Complete'})


@celery.task(bind=True)
def listen_task(task, listen_port, listen_ae_title):
    """
    Background task that listens at a specific port for incoming dicom series
    """

    task_id = task.request.id

    try:
        dicom_connector = DicomConnector(
            port=listen_port, ae_title=listen_ae_title)

        def series_recieved(dicom_path):
            logger.info(
                'Series Recieved at path: {0}'.format(dicom_path))

            # Get the SeriesUID
            series_uid = None
            for f in os.listdir(dicom_path):
                f = os.path.join(dicom_path, f)

                try:
                    d = pydicom.read_file(f)
                    series_uid = d.SeriesInstanceUID
                except Exception as e:
                    logger.debug('No Series UID in: {0}'.format(f))
                    logger.debug(e)

            if series_uid:
                logger.info('Image Series UID: {0}'.format(series_uid))
            else:
                logger.error('Series UID could not be determined... Stopping')
                return

            # Find the data objects with the given series UID and update them
            dos = DataObject.query.filter_by(
                series_instance_uid=series_uid).all()

            if len(dos) == 0:
                logger.error(
                    'No Data Object found with Series UID: {0} ... Stopping'.format(series_uid))
                return

            for do in dos:

                do.is_fetched = True
                do.path = dicom_path
                db.session.commit()

        if not listen_ae_title:
            listen_ae_title = 'PYNETDICOM'

        dicom_connector.listen(series_recieved, ae_title=listen_ae_title)

    except Exception as e:
        logger.error('Listener Error: ' + str(e))

        # Stop the listen task
        celery.control.revoke(task_id, terminate=True)

    return {'status': 'Complete'}


@celery.task(bind=True)
def run_task(task, algorithm_name, config, dataset_id):

    task_id = task.request.id

    # Commit to refresh session
    db.session.commit()

    algorithm = web_app.algorithms[algorithm_name]

    if not config:
        config = algorithm.default_settings

    ds = Dataset.query.filter_by(id=dataset_id).first()
    input_objects = ds.input_data_objects

    state_details = {'current': 0, 'total': len(input_objects),
                     'status': 'Running Algorithm: {0}'.format(algorithm_name)}

    task.update_state(state='RUNNING', meta=state_details)

    if config == None:
        output_data_objects = algorithm.function(
            input_objects, tempfile.mkdtemp())
    else:
        output_data_objects = algorithm.function(
            input_objects, tempfile.mkdtemp(), config)

    if not output_data_objects:
        logger.warning(
            'Algorithm ({0}) did not return any output objects'.format(algorithm_name))

    # Save the data objects
    for do in output_data_objects:
        do.dataset_id = ds.id
        db.session.add(do)
        db.session.commit()

        if do.type == 'DICOM':
            if ds.to_dicom_location:

                logger.info('Sending to Dicom To Location')
                dicom_connector = DicomConnector(host=do.dataset.to_dicom_location.host,
                                                 port=do.dataset.to_dicom_location.port,
                                                 ae_title=do.dataset.to_dicom_location.ae_title)
                dicom_verify = dicom_connector.verify()

                if not dicom_verify:
                    logger.error('Unable to connect to Dicom Location: {0} {1} {2}'.format(
                        do.dataset.to_dicom_location.host,
                        do.dataset.to_dicom_location.port,
                        do.dataset.to_dicom_location.ae_title))
                    continue

                send_result = dicom_connector.send_dcm(do.path)

                if send_result:
                    do.is_sent = True
                    db.session.add(do)
                    db.session.commit()

            else:
                logger.warning(
                    'DICOM Data Object output but not Dicom To location defined in Dataset')

    state_details = {'current': len(input_objects), 'total': len(input_objects),
                     'status': 'Running Algorithm Complete: {0}'.format(algorithm_name)}

    task.update_state(state='COMPLETE', meta=state_details)


def kill_task(task_id):
    """Kills the celery task with the given ID, and removes the link to the endpoint if available"""

    logger.info('Killing task: {0}'.format(task_id))

    endpoint = None
    for e in web_app.data['endpoints']:
        if 'task_id' in e and e['task_id'] == task_id:
            endpoint = e

    celery.control.revoke(task_id, terminate=True)

    if endpoint:
        endpoint.pop('task_id', None)
        web_app.save_data()
