#!/usr/bin/env python

"""
Functionality to update IRR entries in IRRExplorer database via NRTM streaming
"""

import logging

import ipaddr

from irrexplorer import nrtm, irrparser



CREATE_ROUTE   = "SELECT create_route (%s, %s, %s);"
CREATE_AS_SET  = "SELECT create_as_set (%s, %s, %s);"

DELETE_ROUTE   = "DELETE FROM routes  USING sources WHERE routes.route = %s AND routes.asn = %s AND routes.source_id = sources.id AND sources.name = %s;"
DELETE_AS_SET  = "DELETE FROM as_sets USING sources WHERE as_sets.as_macro = %s AND as_sets.source_id = sources.id AND sources.name = %s;"

SELECT_SERIAL  = "SELECT last_seen_serial FROM sources WHERE name = %s"
UPDATE_SERIAL  = "UPDATE sources SET last_seen_serial = %s WHERE sources.name = %s"



class IRRUpdateError(Exception):
    pass



def update_irr(host, port, source, db):

    source = source.lower() # we always lowercase this

    # get serial from database
    cur = db._get_cursor()
    cur.execute(SELECT_SERIAL, (source,))
    srow = cur.fetchall()
    cur.close()

    if not srow:
        raise IRRUpdateError('No serial for source %s found, cannot continue' % source)

    db_serial = srow[0][0]
    serial = int(db_serial) + 1 # don't do the one we last saw

    logging.info('Streaming from %s:%s/%s from serial %s' % (host, port, source, serial))
    c = nrtm.NRTMStreamer(host, source, serial, port)

    stms = []

    changes = {}

    for tag, serial, (obj_type, obj_data) in c.stream():

        if not obj_data:
            continue  # skip over unsupported objects

        obj, data, obj_source = obj_data
        if obj and not obj_source == source:
            logging.info("Weird source difference, skipping: %s vs %s at %s in: %s, %s" % (obj_source, source, serial, obj, data))
            continue

        #print tag, serial, obj_type, obj, source

        if tag == 'ADD':
            if obj_type == irrparser.ROUTE:
                changes['add_route'] = changes.get('add_route', 0) + 1
                # test if this is a proper prefix
                try:
                    ipaddr.IPNetwork(obj, strict=True)
                except ValueError:
                    logging.error('Prefix %s from source %s, is not a proper prefix, skipping object')
                    continue

                # Sometimes (and only sometimes) an ADD will be send for something already exists (I am looking at you altdb)
                # Previously we handled this by deleting the route first. After switching to PostgreSQL we use the upsert feature
                # in the create functions, so no need to handle it here.
                stms.append( ( CREATE_ROUTE, (obj, data, source) ) )

            elif obj_type == irrparser.AS_SET:
                changes['add_as_set'] = changes.get('add_as_set', 0) + 1
                # irrd doesn't seem to generate DEL before updates to as-sets
                # the create function will overwrite the set if it already exists
                stms.append( ( CREATE_AS_SET, (obj, data, source) ) )
            else:
                logging.warning('Weird add %s %s %s' % (tag, serial, obj_type))

        elif tag == 'DEL':
            if obj_type == irrparser.ROUTE:
                changes['del_route'] = changes.get('del_route', 0) + 1
                stms.append( ( DELETE_ROUTE, (obj, data, source) ) )
            elif obj_type == irrparser.AS_SET:
                changes['del_as_set'] = changes.get('del_as_set', 0) + 1
                # this one almost never happens, so it is tricky to test it
                stms.append( ( DELETE_AS_SET, (obj, source) ) )
            else:
                logging.warning('Weird del %s %s %s' % (tag, serial, obj_type))

        elif not tag:
            pass

        else:
            logging.warning('Weird tag: %s %s %s ' % tag, serial, obj_type)

    logging.warning('Changes: %s' % '  '.join( [ '%s: %s' % (k, v) for k,v in changes.items() ] ) )
    if stms:
        # only update serial, if we actually got something
        stms.append( ( UPDATE_SERIAL, (serial, source) ) )

        # debug
        logging.debug("--")
        logging.debug(" Will execute the following statements")
        for stm, arg in stms:
            logging.debug("%s / %s" % (stm, arg))
        logging.debug("--")

    if stms:
        # send delete/insert statements
        cur = db._get_cursor()

        for stm, arg in stms:
            #print stm, arg
            cur.execute(stm, arg)

        db.conn.commit()
        cur.close() # so it doesn't linger while sleeping

        logging.info('IRR update committed and cursor closed')
    else:
        logging.info('No updates for IRR source %s' % source)
