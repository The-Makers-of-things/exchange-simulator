#!/usr/bin/python3
import os
import sys
import logging
import logging.config
import time

from flask import Flask, request, jsonify
from flask_restful import Resource, Api
from werkzeug.exceptions import BadRequest
import json
import redis
from raven.contrib.flask import Sentry


import exchange_api_interface
from exchange import Exchange
from order_book_loader import SimulatorLoader, CoreLoader
import constants
import utils


logger = logging.getLogger(constants.LOGGER_NAME)

app = Flask(__name__)
api = Api(app)


class LiquiTrade(Resource):

    def post(self):
        """A 'requests' dictionary is made that has the Post Request received
        in the web service and the appropriate parse """

        post_reqs = {
            "Trade": {
                "params_method": exchange_api_interface.TradeParams,
                "exchange_method": exchange_caller.execute_trade_api},
            "WithdrawCoin": {
                "params_method": exchange_api_interface.WithdrawParams,
                "exchange_method": exchange_caller.withdraw_api},
            "getInfo": {
                "params_method": exchange_api_interface.GetBalanceParams,
                "exchange_method": exchange_caller.get_balances_api}
            # "CancelOrder": {
            #    "params_method": exchange_api_interface.CancelTradeParams}
            #    "exchange_method":exchange_caller.==MISSING_METHOD==},
            # "ActiveOrders": ,
            # "OrderHistory":,
            # "OrderInfo":,
            # "TradeHistory":,

        }

        if "Key" not in request.headers:
            logger.error("Missing key in header")
            return jsonify({
                "success": 0,
                "error": "Missing 'Key' Header"
            })

        request_all = request.form.to_dict()
        logger.info("Original params: %s", request_all)

        timestamp = request.args.get('timestamp')
        if not timestamp:
            timestamp = int(time.time() * 1000)
        request_all['timestamp'] = timestamp

        try:
            method = request_all["method"]
        except KeyError:
            logger.error("Missing method")
            return jsonify({
                "success": 0,
                "error": "Method is missing in your request"
            })

        request_all["api_key"] = request.headers["key"].lower()
        to_exchange_results = exchange_parser.parse_to_exchange(
            method, request_all)

        if "error" in to_exchange_results:
            response = to_exchange_results
        else:
            try:
                exchange_params = post_reqs[method]["params_method"](
                    **to_exchange_results)
                exchange_caller.before_api(
                    request.headers["key"].lower())
                exchange_reply = post_reqs[method]["exchange_method"](
                    exchange_params)
                exchange_caller.after_api(
                    request.headers["key"].lower())
                exchange_parsed_reply = (exchange_parser.parse_from_exchange(
                    method, exchange_reply))
                response = exchange_parsed_reply
            except Exception as e:
                logger.error(e)
                response = {
                    'success': 0,
                    'error': str(e)
                }

        logger.info("Response: %s", response)
        return jsonify(response)


api.add_resource(LiquiTrade, "/")


@app.route("/depth/<string:pairs>", methods=['GET'])
def depth(pairs):
    timestamp = request.args.get('timestamp')
    if timestamp:
        timestamp = int(timestamp)
    else:
        timestamp = int(time.time() * 1000)

    try:
        depth = exchange_caller.get_depth(pairs, timestamp)
        return json.dumps(depth)
    except ValueError as e:
        logger.info("Bad Request: {}".format(e))
        return BadRequest()


if __name__ == "__main__":
    mode = os.environ.get('KYBER_ENV', 'dev')
    logging.config.fileConfig('logging.conf')

    rdb = utils.get_redis_db()

    if mode == 'simulation':
        data_imported = rdb.get('IMPORTED_SIMULATION_DATA')

        if not data_imported:
            logger.info('Import simulation data ...')
            ob_file = 'data/full_ob'
            # ob_file = 'data/sample_ob'
            try:
                utils.copy_order_books_to_db(ob_file, rdb)
            except FileNotFoundError:
                sys.exit('Data is missing.')
            rdb.set('IMPORTED_SIMULATION_DATA', 1)
            logger.info('Finish setup process.')

        order_book_loader = SimulatorLoader(rdb)
    else:
        order_book_loader = CoreLoader()

    exchange_parser = exchange_api_interface.LiquiApiInterface()
    exchange_caller = Exchange(
        "liqui",
        [constants.KNC, constants.ETH, constants.OMG],
        rdb,
        order_book_loader,
        constants.LIQUI_ADDRESS,
        constants.BANK_ADDRESS,
        5 * 60
    )

    if mode != 'dev':
        sentry = Sentry(app, dsn='https://c2c05c37737d4c0a9e75fc4693005c2c:'
                        '17e24d6686d34465b8a97801e6e31ba4@sentry.io/241770')

    app.run(host='0.0.0.0', port='5000')
