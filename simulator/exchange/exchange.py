#!/usr/bin/python3
import time
import traceback

from .. import web3_interface, utils, config
from ..order import Order


logger = utils.get_logger()


class Exchange:

    def __init__(self, exchange_name, private_key,
                 supported_tokens, db,
                 order_handler, balance_handler,
                 deposit_address):
        self.name = exchange_name
        self.supported_tokens = supported_tokens
        self.db = db
        self.balance = balance_handler
        self.orders = order_handler
        self.deposit_address = deposit_address
        self.private_key = private_key
        self.last_check = 0

    def _update_balance(func):
        def wrapper(self, api_key, *args, **kargs):
            current = utils.get_timestamp()
            if self.last_check + config.DELAY < current:
                logger.debug('Check deposit now')
                try:
                    # process withdraw
                    # 1. check deposit
                    self.check_deposits(api_key)
                    # TODO 2. match open orders
                    # TODO 3. process withdraw request
                    self.last_check = current
                except Exception as e:
                    traceback.print_exc()
                    logger.error('Handle deposit fail: {}'.format(e))
            return func(self, api_key, *args, **kargs)
        return wrapper

    @_update_balance
    def get_balance(self, api_key, blc_types=['available', 'lock']):
        return {t: self.balance.get(api_key, t) for t in blc_types}

    def check_pair(self, pair):
        base, quote = pair.split('_')
        base_is_supported = any(base == t.token for t in self.supported_tokens)
        quote_is_ether = quote == 'eth'
        if (not base_is_supported) or (not quote_is_ether):
            raise ValueError('Invalid pair {}.'.format(pair))

    def get_order_book(self, pair, timestamp):
        self.check_pair(pair)
        try:
            order_book = self.orders.load(pair, self.name, timestamp)
        except Exception as e:
            logger.info('Order book {}_{} is missing: {}'.format(
                pair, timestamp, e))
            order_book = {'Asks': [], 'Bids': []}

        # logger.debug("Order Book: {}".format(order_book))
        return order_book

    def trade(self, api_key, type, rate, pair, amount, timestamp):
        rate, amount = float(rate), float(amount)
        self.check_pair(pair)
        base, quote = pair.split('_')
        type = type.lower()

        # 1. lock balance
        if type == 'buy':
            locked = rate * amount
            self.balance.lock(api_key, quote, locked)
        elif type == 'sell':
            locked = amount
            self.balance.lock(api_key, base, locked)
        else:
            raise ValueError('Invalid type of order.')

        # 2. open new order
        new_order = Order(pair, type, rate, amount)

        # 3. match new order
        base_change, quote_change = self._match_order(api_key,
                                                      type,
                                                      rate,
                                                      pair,
                                                      amount,
                                                      timestamp)

        # 4.1. update order
        new_order.executed_amount = base_change
        new_order.remaining_amount = amount - base_change
        self.orders.add(new_order)
        # 4.2. update balance
        if new_order.executed_amount > 0:
            if type == 'buy':
                self.balance.deposit(api_key, base, base_change, 'available')
                self.balance.withdraw(api_key, quote, quote_change, 'lock')
            else:
                self.balance.deposit(api_key, quote, quote_change, 'available')
                self.balance.withdraw(api_key, base, base_change, 'lock')

        if not new_order.active():
            if type == 'buy':
                self.balance.unlock(api_key, quote, locked - quote_change)
            else:
                self.balance.unlock(api_key, base, locked - base_change)

        return {
            'received': new_order.executed_amount,
            'remaining': new_order.remaining_amount,
            'order_id': new_order.id
        }

    def _match_order(self, api_key, type, rate, pair, amount, timestamp):
        order_book = self.get_order_book(pair, timestamp)
        base, quote = pair.split('_')  # e.g. knc_eth -> base=knc, quote=eth
        if type == 'buy':
            orders = order_book['Asks']
        elif type == 'sell':
            orders = order_book['Bids']

        base_change, quote_change = 0.0, 0.0
        for order in orders:
            logger.debug('Processing order: {}'.format(order))

            # id = get_order_id(pair, order['Rate'], order['Quantity'])
            # if id in self.processed_order_ids:
            # continue  # order is already processed, continue to next order

            bad_rate = (type == 'buy' and order['Rate'] > rate) or (
                type == 'sell' and order['Rate'] < rate)
            if bad_rate:
                break  # cant get better rate -> exist

            needed_quantity = amount - base_change
            trade_amount = min(order['Quantity'], needed_quantity)

            logger.debug(
                'Execute this order with quantity {}'.format(trade_amount))

            base_change += trade_amount
            quote_change += order['Rate'] * trade_amount

            # self.processed_order_ids.add(id)
            if needed_quantity == trade_amount:
                break  # trade request has been fulfilled

        logger.debug('Base change, Quote change: {}, {}'.format(
            base_change, quote_change))

        return base_change, quote_change

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def get_active_orders(self, pair):
        if not pair:
            pair = None
        else:
            self.check_pair(pair)
        return self.orders.get_all(pair)

    def cancel_order(self, api_key, order_id):
        order = self.orders.get(order_id)
        base, quote = order.pair.split('_')
        # unlock balance
        if order.type == 'buy':
            self.balance.unlock(
                api_key, quote, order.remaining_amount * order.rate)
        else:
            self.balance.unlock(api_key, base, order.remaining_amount)
        self.orders.remove(order_id)

    def check_deposits(self, api_key):
        token_addresses = [t.address for t in self.supported_tokens]
        deposits = web3_interface.get_balances(self.deposit_address,
                                               token_addresses)
        pending_tnx = utils.get_pending_tnx(exchange=self.name)
        if(sum(deposits) > 0):
            logger.debug('Got deposit: {}'.format(deposits))
            tx = web3_interface.clear_deposits(self.private_key,
                                               self.deposit_address,
                                               token_addresses,
                                               deposits)

        for idx, deposit in enumerate(deposits):
            token = self.supported_tokens[idx]
            qty = float(deposit) / (10**token.decimals)
            if qty > 0:
                self.balance.deposit(api_key, token.token, qty, 'available')
                self.complete_tnx(qty, pending_tnx.get(token.token, []), token)

    def complete_tnx(self, total_qty, pending_tnx, token):
        history = self.balance.get_history('deposit')
        for tnx in pending_tnx:
            if tnx['tx'] in history:
                continue
            amount = float(tnx['amount'])
            if total_qty > amount:
                total_qty -= amount
                self.balance.add_activity('deposit',
                                          amount,
                                          'address',
                                          tnx['tx'],
                                          token.token)
            if total_qty == 0:
                break

    def withdraw(self, api_key, coinName, address, amount):
        coinName = coinName.lower()
        amount = float(amount)
        token = utils.get_token(coinName)
        tx = web3_interface.withdraw(self.private_key,
                                     self.deposit_address,
                                     token.address,
                                     int(amount * 10**token.decimals),
                                     address)
        self.balance.withdraw(user=api_key, token=coinName,
                              amount=amount, balance_type='available')
        try:
            self.balance.add_activity('withdraw',
                                      amount,
                                      address,
                                      tx,
                                      token.token)
        except Exception as e:
            logger.error('Add withdraw history failed: {}'.format(e))
        return tx


MAX_ORDER_ID = 2 ** 31


def get_order_id(pair, rate, quantity):
    """Create Id for an order by hashing a string contain
    pair, it's rate and quantity
    """
    keys = [pair, rate, quantity]
    return hash('.'.join(map(str, keys))) % MAX_ORDER_ID
