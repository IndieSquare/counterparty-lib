#! /usr/bin/python3

"""
Broadcast a message, with or without a price.

Multiple messages per block are allowed. Bets are be made on the ‘timestamp’
field, and not the block index.

An address is a feed of broadcasts. Feeds (addresses) may be locked with a
broadcast containing a blank ‘text’ field. Bets on a feed reference the address
that is the source of the feed in an output which includes the (latest)
required fee.

Broadcasts without a price may not be used for betting. Broadcasts about events
with a small number of possible outcomes (e.g. sports games), should be
written, for example, such that a price of 1 XCP means one outcome, 2 XCP means
another, etc., which schema should be described in the ‘text’ field.

fee_multipilier: .05 XCP means 5%. It may be greater than 1, however; but
because it is stored as a four‐byte integer, it may not be greater than about
42.
"""

import struct
import decimal
D = decimal.Decimal
import logging

from . import (util, config, bitcoin)

FORMAT = '>IdI40p' # How many characters *can* the text be?! (That is, how long is PREFIX?!)
ID = 30
LENGTH = 4 + 8 + 4 + 40

def create (db, source, timestamp, value, fee_multiplier, text, test=False):

    broadcasts = util.get_broadcasts(db, validity='Valid', source=source, order_by='tx_index ASC')
    if broadcasts:
        last_broadcast = broadcasts[-1]
        if not last_broadcast['text']:
            raise exceptions.UselessError('Locked feed')
        elif timestamp <= last_broadcast['timestamp']:
            raise exceptions.UselessError('Feed timestamps must be monotonically increasing')

    data = config.PREFIX + struct.pack(config.TXTYPE_FORMAT, ID)
    data += struct.pack(FORMAT, timestamp, value, fee_multiplier,
                        text.encode('utf-8'))
    return bitcoin.transaction(source, None, None, config.MIN_FEE, data, test)

def parse (db, tx, message):
    broadcast_parse_cursor = db.cursor()
    # Ask for forgiveness…
    validity = 'Valid'

    # Unpack message.
    try:
        timestamp, value, fee_multiplier, text = struct.unpack(FORMAT, message)
        text = text.decode('utf-8')
    except Exception:
        timestamp, value, fee_multiplier, text = None, None, None, None
        validity = 'Invalid: could not unpack'

    broadcasts = util.get_broadcasts(db, validity='Valid', source=tx['source'], order_by='tx_index ASC')
    if broadcasts:
        last_broadcast = broadcasts[-1]
        if not last_broadcast['text']:
            validity = 'Invalid: locked feed'
        elif not timestamp > last_broadcast['timestamp']:
            validity = 'Invalid: feed timestamps must be monotonically increasing'

    # Add parsed transaction to message‐type–specific table.
    broadcast_parse_cursor.execute('''INSERT INTO broadcasts(
                        tx_index,
                        tx_hash,
                        block_index,
                        source,
                        timestamp,
                        value,
                        fee_multiplier,
                        text,
                        validity) VALUES(?,?,?,?,?,?,?,?,?)''',
                        (tx['tx_index'],
                        tx['tx_hash'],
                        tx['block_index'],
                        tx['source'],
                        timestamp,
                        value,
                        fee_multiplier,
                        text,
                        validity)
                  )

    # Log.
    if validity == 'Valid':
        if not text:
            logging.info('Broadcast: {} locked his feed.'.format(tx['source'], util.short(tx['tx_hash'])))
        else:
            if not value: infix = '‘' + text + '’'
            else: infix = '‘' + text + ' = ' + str(value) + '’'
            suffix = ' from ' + tx['source'] + ' at ' + util.isodt(timestamp) + ' (' + util.short(tx['tx_hash']) + ')'
            logging.info('Broadcast: {}'.format(infix + suffix))


    # Handle bet matches that use this feed.
    broadcast_parse_cursor.execute('''SELECT * FROM bet_matches \
                      WHERE (validity=? AND feed_address=?)
                      ORDER BY tx1_index ASC, tx0_index ASC''', ('Valid', tx['source']))
    for bet_match in broadcast_parse_cursor.fetchall():
        broadcast_bet_match_cursor = db.cursor()
        validity = 'Valid'
        bet_match_id = bet_match['tx0_hash'] + bet_match['tx1_hash']

        # Get known bet match type IDs.
        cfd_type_id = util.BET_TYPE_ID['BullCFD'] + util.BET_TYPE_ID['BearCFD']
        equal_type_id = util.BET_TYPE_ID['Equal'] + util.BET_TYPE_ID['NotEqual']

        # Get the bet match type ID of this bet match.
        bet_match_type_id = bet_match['tx0_bet_type'] + bet_match['tx1_bet_type']

        # Contract for difference, with determinate settlement date.
        if validity == 'Valid' and bet_match_type_id == cfd_type_id:

            # Recognise tx0, tx1 as the bull, bear (in the right direction).
            if bet_match['tx0_bet_type'] < bet_match['tx1_bet_type']:
                bull_address = bet_match['tx0_address']
                bear_address = bet_match['tx1_address']
                bull_escrow = bet_match['forward_amount']
                bear_escrow = bet_match['backward_amount']
            else:
                bull_address = bet_match['tx1_address']
                bear_address = bet_match['tx0_address']
                bull_escrow = bet_match['backward_amount']
                bear_escrow = bet_match['forward_amount']

            leverage = D(bet_match['leverage']) / 5040
            initial_value = bet_match['initial_value']
                
            total_escrow = bull_escrow + bear_escrow
            bear_credit = round(bear_escrow - D(value - initial_value) * leverage * config.UNIT)
            bull_credit = total_escrow - bear_credit

            if bet_match['validity'] == 'Valid':
                # Liquidate, as necessary.
                if bull_credit >= total_escrow:
                    util.credit(db, bull_address, 'XCP', total_escrow)
                    validity = 'Force‐Liquidated Bear'
                    logging.info('Contract Force‐Liquidated: {} XCP credited to the bull, and 0 XCP credited to the bear ({})'.format(util.devise(db, total_escrow, 'XCP', 'output'), util.short(bet_match_id)))
                elif bull_credit <= 0:
                    util.credit(db, bear_address, 'XCP', total_escrow)
                    validity = 'Force‐Liquidated Bull'
                    logging.info('Contract Force‐Liquidated: 0 XCP credited to the bull, and {} XCP credited to the bear ({})'.format(util.devise(db, total_escrow, 'XCP', 'output'), util.short(bet_match_id)))

            # Settle.
            if validity == 'Valid' and timestamp >= bet_match['deadline']:
                util.credit(db, bull_address, 'XCP', bull_credit)
                util.credit(db, bear_address, 'XCP', bear_credit)
                validity = 'Settled (CFD)'
                logging.info('Contract Settled: {} XCP credited to the bull, and {} XCP credited to the bear ({})'.format(bull_credit / config.UNIT, bear_credit / config.UNIT, util.short(bet_match_id)))

        # Equal[/NotEqual] bet.
        if validity == 'Valid' and  bet_match_type_id == equal_type_id and timestamp >= bet_match['deadline']:

            # Recognise tx0, tx1 as the bull, bear (in the right direction).
            if bet_match['tx0_bet_type'] < bet_match['tx1_bet_type']:
                equal_address = bet_match['tx0_address']
                notequal_address = bet_match['tx1_address']
            else:
                equal_address = bet_match['tx1_address']
                notequal_address = bet_match['tx0_address']

            # Decide who won.
            total_escrow = bet_match['forward_amount'] + bet_match['backward_amount']
            if value == bet_match['target_value']:
                winner = 'Equal'
                util.credit(db, equal_address, 'XCP', total_escrow)
                validity = 'Settled for Equal'
            else:
                winner = 'NotEqual'
                util.credit(db, notequal_address, 'XCP', total_escrow)
                validity = 'Settled for NotEqual'

            logging.info('Contract Settled: {} won ({})'.format(winner, util.short(bet_match_id)))

        # Update the bet match’s status.
        broadcast_bet_match_cursor.execute('''UPDATE bet_matches \
                          SET validity=? \
                          WHERE (tx0_hash=? and tx1_hash=?)''',
                      (validity, bet_match['tx0_hash'], bet_match['tx1_hash']))
        broadcast_bet_match_cursor.close()

    broadcast_parse_cursor.close()
       
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
