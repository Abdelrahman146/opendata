# -*- coding: utf-8 -*-
'''
Provide some Quality Assurance by scoring datasets against Sir Tim
Berners-Lee\'s five stars of openness
'''
import datetime
import json
import os
import traceback

import ckan.lib.celery_app as celery_app
from ckan.plugins import toolkit
import ckan.lib.helpers as ckan_helpers
from ckanext.qa.sniff_format import sniff_file_format
from ckanext.qa import lib
from ckanext.archiver.model import Archival, Status


class QAError(Exception):
    pass

# Description of each score, used elsewhere
OPENNESS_SCORE_DESCRIPTION = {
    0: 'Not obtainable or license is not open',
    1: 'Obtainable and open license',
    2: 'Machine readable format',
    3: 'Open and standardized format',
    4: 'Ontologically represented',
    5: 'Fully Linked Open Data as appropriate',
}


def load_config(ckan_ini_filepath):
    import paste.deploy
    config_abs_path = os.path.abspath(ckan_ini_filepath)
    conf = paste.deploy.appconfig('config:' + config_abs_path)
    import ckan
    ckan.config.environment.load_environment(conf.global_conf,
                                             conf.local_conf)


def register_translator():
    # Register a translator in this thread so that
    # the _() functions in logic layer can work
    from paste.registry import Registry
    from pylons import translator
    from ckan.lib.cli import MockTranslator
    global registry
    registry = Registry()
    registry.prepare()
    global translator_obj
    translator_obj = MockTranslator()
    registry.register(translator, translator_obj)


@celery_app.celery.task(name="qa.update_package")
def update_package(ckan_ini_filepath, package_id):
    """
    Given a package, calculates an openness score for each of its resources.
    It is more efficient to call this than 'update' for each resource.

    Returns None
    """
    log = update_package.get_logger()
    load_config(ckan_ini_filepath)
    register_translator()

    try:
        update_package_(package_id, log)
    except Exception, e:
        log.error('Exception occurred during QA update_package: %s: %s',
                  e.__class__.__name__,  unicode(e))
        raise


def update_package_(package_id, log):
    from ckan import model
    package = model.Package.get(package_id)
    if not package:
        raise QAError('Package ID not found: %s' % package_id)

    log.info('Openness scoring package %s (%i resources)', package.name,
             len(package.resources))

    for resource in package.resources:
        qa_result = resource_score(resource, log)
        log.info('Openness scoring: \n%r\n%r\n%r\n\n', qa_result, resource,
                 resource.url)
        save_qa_result(resource, qa_result, log)
        log.info('CKAN updated with openness score')

    # Refresh the index for this dataset, so that it contains the latest
    # qa info
    _update_search_index(package.id, log)


@celery_app.celery.task(name="qa.update")
def update(ckan_ini_filepath, resource_id):
    """
    Given a resource, calculates an openness score.

    Returns a JSON dict with keys:

        'openness_score': score (int)
        'openness_score_reason': the reason for the score (string)
    """
    log = update.get_logger()
    load_config(ckan_ini_filepath)
    register_translator()
    try:
        update_resource_(resource_id, log)
    except Exception, e:
        log.error('Exception occurred during QA update_resource: %s: %s',
                  e.__class__.__name__,  unicode(e))
        raise


def update_resource_(resource_id, log):
    from ckan import model
    resource = model.Resource.get(resource_id)
    if not resource:
        raise QAError('Resource ID not found: %s' % resource_id)
    qa_result = resource_score(resource, log)
    log.info('Openness scoring: \n%r\n%r\n%r\n\n', qa_result, resource,
             resource.url)
    save_qa_result(resource, qa_result, log)
    log.info('CKAN updated with openness score')

    if toolkit.check_ckan_version(max_version='2.2.99'):
        package = resource.resource_group.package
    else:
        package = resource.package
    if package:
        # Refresh the index for this dataset, so that it contains the latest
        # qa info
        _update_search_index(package.id, log)
    else:
        log.warning('Resource not connected to a package. Res: %r', resource)
    return json.dumps(qa_result)


def get_qa_format(resource_id):
    '''Returns the format of the resource, as recorded in the QA table.'''
    from ckanext.qa.model import QA
    q = QA.get_for_resource(resource_id)
    if not q:
        return ''
    return q.format


def format_get(key):
    '''Returns a resource format, as defined in ckan.

    :param key: format extension / mimetype / title e.g. 'CSV',
                'application/msword', 'Word document'
    :param key: string
    :returns: format string
    '''
    format_tuple = ckan_helpers.resource_formats().get(key.lower())
    if not format_tuple:
        return
    return format_tuple[1]  # short name


def resource_score(resource, log):
    """
    Score resource on Sir Tim Berners-Lee\'s five stars of openness.

    Returns a dict with keys:

        'openness_score': score (int)
        'openness_score_reason': the reason for the score (string)
        'format': format of the data (string)
        'archival_timestamp': time of the archival that this result is based on (iso string)

    Raises QAError for reasonable errors
    """
    score = 0
    score_reason = ''
    format_ = None

    try:
        score_reasons = []  # a list of strings detailing how we scored it
        archival = Archival.get_for_resource(resource_id=resource.id)
        if not resource:
            raise QAError('Could not find resource "%s"' % resource.id)

        score, format_ = score_if_link_broken(archival, resource, score_reasons, log)
        if score == None:
            # we don't want to take the publisher's word for it, in case the link
            # is only to a landing page, so highest priority is the sniffed type
            score, format_ = score_by_sniffing_data(archival, resource,
                                                    score_reasons, log)
            if score == None:
                # Fall-backs are user-given data
                score, format_ = score_by_url_extension(resource, score_reasons, log)
                if score == None:
                    score, format_ = score_by_format_field(resource, score_reasons, log)
                    if score == None:
                        log.warning('Could not score resource: "%s" with url: "%s"',
                                    resource.id, resource.url)
                        score_reasons.append('No se pudo entender el formato del archivo, por lo tanto la puntuacion es 1.')
                        score = 1
                        if format_ == None:
                            # use any previously stored format value for this resource
                            format_ = get_qa_format(resource.id)
        score_reason = ' '.join(score_reasons)
        format_ = format_ or None
    except Exception, e:
        log.error('Unexpected error while calculating openness score %s: %s\nException: %s', e.__class__.__name__,  unicode(e), traceback.format_exc())
        score_reason = "Error desconocido: %s" % str(e)
        raise

    # Even if we can get the link, we should still treat the resource
    # as having a score of 0 if the license isn't open.
    #
    # It is important we do this check after the link check, otherwise
    # the link checker won't get the chance to see if the resource
    # is broken.
    if toolkit.check_ckan_version(max_version='2.2.99'):
        package = resource.resource_group.package
    else:
        package = resource.package
    if score > 0 and not package.isopen():
        score_reason = 'Licencia no abierta'
        score = 0

    log.info('Score: %s Reason: %s', score, score_reason)

    archival_updated = archival.updated.isoformat() \
        if archival and archival.updated else None
    result = {
        'openness_score': score,
        'openness_score_reason': score_reason,
        'format': format_,
        'archival_timestamp': archival_updated
    }

    return result


def broken_link_error_message(archival):
    '''Given an archival for a broken link, it returns a helpful
    error message (string) describing the attempts.'''
    def format_date(date):
        if date:
            return date.strftime('%Y/%m/%d')
        else:
            return ''
    messages = ['El fichero no puso ser descargado.',
                'Problema: %s.' % archival.status,
                'Detalles del error: %s.' % archival.reason,
                'intentado:  %s.' % format_date(archival.updated)]
    last_success = format_date(archival.last_success)
    if archival.failure_count == 1:
        if last_success:
            messages.append('Esta URL funciono la vez anterior: %s.' % last_success)
        else:
            messages.append('Este fue el primer intento.')
    else:
        messages.append('Intentado %s veces desde %s.' % \
                        (archival.failure_count,
                         format_date(archival.first_failure)))
        if last_success:
            messages.append('La ultima vez que ha funcionado esta herramienta fue en: %s.' % last_success)
        else:
            messages.append('Esta URL no ha funcionado en la historia de esta herramienta.')
    return ' '.join(messages)


def score_if_link_broken(archival, resource, score_reasons, log):
    '''
    Looks to see if the archiver said it was broken, and if so, writes to
    the score_reasons and returns a score.

    Return values:
      * Returns a tuple: (score, format_)
      * score is an integer or None if it cannot be determined
      * format_ is a string or None
      * is_broken is a boolean
    '''
    if archival and archival.is_broken:
        # Score 0 since we are sure the link is currently broken
        score_reasons.append(broken_link_error_message(archival))
        format_ = get_qa_format(resource.id)
        log.info('Archiver says link is broken. Previous format: %r' % format_)
        return (0, format_)
    return (None, None)

def score_by_sniffing_data(archival, resource, score_reasons, log):
    '''
    Looks inside a data file\'s contents to determine its format and score.

    It adds strings to score_reasons list about how it came to the conclusion.

    Return values:
      * It returns a tuple: (score, format_string)
      * If it cannot work out the format then format_string is None
      * If it cannot score it, then score is None
    '''
    if not archival or not archival.cache_filepath:
        score_reasons.append('este fichero no ha sido puntuado en el momento de descargarlo.')
        return (None, None)
    # Analyse the cached file
    filepath = archival.cache_filepath
    if not os.path.exists(filepath):
        score_reasons.append('Cache filepath does not exist: "%s".' % filepath)
        return (None, None)
    else:
        if filepath:
            sniffed_format = sniff_file_format(filepath, log)
            score = lib.resource_format_scores().get(sniffed_format['format']) \
                if sniffed_format else None
            if sniffed_format:
                score_reasons.append('El contenido del archivo parece ser formato "%s" que recibe la puntuacion de nivel de apertura: %s.' % (sniffed_format['format'], score))
                return score, sniffed_format['format']
            else:
                score_reasons.append('El formato del archivo no se reconoce a partir de su contenido.')
                return (None, None)
        else:
            # No cache_url
            if archival.status_id == Status.by_text('Chose not to download'):
                score_reasons.append('El archivo no se ha descargado deliberadamente. Razon: %s. Usando otro metodo para determinar el nivel de apertura.' % \
                                     archival.reason)
                return (None, None)
            elif archival.is_broken is None and archival.status_id:
                # i.e. 'Download failure' or 'System error during archival'
                score_reasons.append('A ocurrido un error de sistema descargando este fichero. Razon: %s. Usando otro metodo para determinar el nivel de apertura.' % \
                                     archival.reason)
                return (None, None)
            else:
                score_reasons.append('Este archivo no se ha descargado en el momento de puntuarlo.')
                return (None, None)


def score_by_url_extension(resource, score_reasons, log):
    '''
    Looks at the URL for a resource to determine its format and score.

    It adds strings to score_reasons list about how it came to the conclusion.

    Return values:
      * It returns a tuple: (score, format_string)
      * If it cannot work out the format then format is None
      * If it cannot score it, then score is None
    '''
    extension_variants_ = extension_variants(resource.url.strip())
    if not extension_variants_:
        score_reasons.append('No puede determinar la extension del fichero de la URL.')
        return (None, None)
    for extension in extension_variants_:
        format_ = format_get(extension)
        if format_:
            score = lib.resource_format_scores().get(format_)
            if score:
                score_reasons.append('La extension "%s" de la url se relaciona con el formato "%s" y recibe una puntuacion de: %s.' % (extension, format_, score))
                return score, format_
            else:
                score = 1
                score_reasons.append('La extension de "%s" de la url se relaciones con el formato "%s" pero no hay configurada una extension para este formato por lo que se le da una puntuacion por de fecto %s.' % (extension, format_, score))
                return score, format_
        score_reasons.append('La extension "%s" de la URL es un formato desconocido.' % extension)
    return (None, None)

def extension_variants(url):
    '''
    Returns a list of extensions, in order of which would more
    significant.

    >>> extension_variants('http://dept.gov.uk/coins.data.1996.csv.zip')
    ['csv.zip', 'zip']
    >>> extension_variants('http://dept.gov.uk/data.csv?callback=1')
    ['csv']
    '''
    url = url.split('?')[0] # get rid of params
    url = url.split('/')[-1] # get rid of path - leaves filename
    split_url = url.split('.')
    results = []
    for number_of_sections in [2, 1]:
        if len(split_url) > number_of_sections:
            results.append('.'.join(split_url[-number_of_sections:]))
    return results


def score_by_format_field(resource, score_reasons, log):
    '''
    Looks at the format field of a resource to determine its format and score.

    It adds strings to score_reasons list about how it came to the conclusion.

    Return values:
      * It returns a tuple: (score, format_string)
      * If it cannot work out the format then format_string is None
      * If it cannot score it, then score is None
    '''
    format_field = resource.format or ''
    if not format_field:
        score_reasons.append('El campo de formato esta en blanco.')
        return (None, None)
    format_tuple = ckan_helpers.resource_formats().get(format_field.lower()) or \
        ckan_helpers.resource_formats().get(lib.munge_format_to_be_canonical(format_field))
    if not format_tuple:
        score_reasons.append('El campo de formato "%s" no corresponde a un formato conocido.' % format_field)
        return (None, None)
    score = lib.resource_format_scores().get(format_tuple[1])
    print ("El valor de score %s-----------------------------------------:" % score)
    print ("El valor de format_tuple[1] %s-----------------------------------------:" % format_tuple[1])

    score_reasons.append('El campo de formato "%s" recibe una puntuacion: %s.' %
                         (format_field, score))
    return (score, format_tuple[1])


def _update_search_index(package_id, log):
    '''
    Tells CKAN to update its search index for a given package.
    '''
    from ckan import model
    from ckan.lib.search.index import PackageSearchIndex
    package_index = PackageSearchIndex()
    context_ = {'model': model, 'ignore_auth': True, 'session': model.Session,
                'use_cache': False, 'validate': False}
    package = toolkit.get_action('package_show')(context_, {'id': package_id})
    package_index.index_package(package, defer_commit=False)
    log.info('Search indexed %s', package['name'])


def save_qa_result(resource, qa_result, log):
    """
    Saves the results of the QA check to the qa table.
    """
    import ckan.model as model
    from ckanext.qa.model import QA

    now = datetime.datetime.now()

    qa = QA.get_for_resource(resource.id)
    if not qa:
        qa = QA.create(resource.id)
        model.Session.add(qa)
    else:
        log.info('QA from before: %r', qa)

    for key in ('openness_score', 'openness_score_reason', 'format'):
        setattr(qa, key, qa_result[key])
    qa.archival_timestamp = qa_result['archival_timestamp']
    qa.updated = now

    model.Session.commit()

    log.info('QA results updated ok')
    return qa  # for tests
