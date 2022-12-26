# Copyright 2022 Sandy Pyke - spyke@btdbot.com
#
# Licensed under the Apache License, Version 2.0 (the "License")
# with the “Commons Clause” License Condition v1.0 (the Condition);
# you may not use this file except in compliance with the License
# and the Condition.
#
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# You may obtain a copy of the Condition at
#
#   https://commonsclause.com/
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License and the Condition is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the License and the Condition for the specific language governing
# permissions and limitations under the License.

import os
import sys
import logging
import time
import json
import math
from telethon import TelegramClient
from telethon.events import NewMessage
from telethon.errors import SessionPasswordNeededError

from dydx3 import Client as dydx3_client
from dydx3 import constants as dydx3_constants
from dydx3.helpers import request_helpers as dydx3_request_helpers

### Global variables ###

config = {}
state = {}
logger = None
dydx_api = None
telegram_api = None

### Global bot settings ###

settings = {

    # Bot identification
    'bot_version' : 'V0.1',
    'bot_short_name' : 'btdBotClient',
    'bot_long_name' : 'Buy The Dip BOT client for dYdX',

    # This asset will be monitored for trding signals, it will be combined with
    # the quote asset to build the pair name. This algorithm will be in position
    # most of the time, so this is not compatible with trading multiple assets in
    # parallel
    'base_asset' : 'BTC',

    # Our base currency against which the above assets will be measured
    'quote_asset' : 'USD',

    # Connect to dydx's mainnet or testnetss
    # Make sure environment variables match selected network...
    #'dydx_network' : 'mainnet',
    'dydx_network' : 'testnet',

    # Our desired IDs
    'chat_id' : -1001552279905,
    'bot_id' : 5861320113,
}

### File names ###

# File where log output will be saved
settings['log_file'] = settings['bot_short_name'] + '.log'      # Name of our log file

# Our config file, we store varibles here so that the user can adjust on the fly to avoid
# having to restart the bot
settings['config_file'] = settings['bot_short_name'] + '_config.json'

# File where bot state is saved on exit
settings['state_file'] = settings['bot_short_name'] + '_state.json'

# The bot will store the telegram chat id in this file, so the user does not need to resend
# a start command everytime the bot is re-started
settings['telegram_chat_id_file'] = settings['bot_short_name'] + '_chatId.json'

# Dump file for dumping the internal dataframes for all trade pairs so we can see what the
# bot is thinking. Don't include the extention as we may dump in a few different formats.
settings['dump_file'] = settings['bot_short_name'] + '_dump'

### Bot defaults ###

# Default config values
default_config = {
    # Use this to disable trading when we need to test something
    'enable_trading' : True,

    # Percent of balance to use for trading with 1.0 = 100%
    # Set this to 1.0 or less to control how much equity the bot should use
    'trade_size_factor' : 0.95,
}

# Default state variables
default_state = {
    'bot_run' : True,
    'bot_init' : True,
    'current_total_balance' : -1.0,
    'current_quote_balance' : -1.0,
    'starting_balance' : -1.0,
    'trade_pair' : '',
    'pair_info' : {},
    'open_pos_info' : [],
    'open_trades' : 0,
    'exit_order_id' : None,
    'dydx_pos_id' : 0,
}

### Helper classes ###

class logger_writer:
    # This class will write anything sent to stdout or stderr to our logger
    # We should use the log_info, log_debug and log_error functions for our
    # normal logging
    def __init__(self, logger, level):
       self.logger = logger
       self.level = level
       self.linebuf = ''

    def write(self, buf):
       for line in buf.rstrip().splitlines():
           self.logger.log(self.level, log_add_utc_time(line.rstrip()))
       
    def flush(self):
        pass

class one_line_exception_formatter(logging.Formatter):
    # Class to handle formating multi-line exceptions into single lines for logs
    def format_exception(self, exc_info):
        result = super().format_exception(exc_info)
        return repr(result)
 
    def format(self, record):
        result = super().format(record)
        if record.exc_text:
            result = result.replace("\n", "")
        return result

### Helper functions ###

def update_current_balance():
    global state

    response = dydx_api.private.get_accounts()
    account = response.data['accounts'][0]
    state['current_total_balance'] = float(account['equity'])
    log_debug(f"Current balance = {state['current_total_balance']} {settings['quote_asset']}")

def get_position_size():
    # TODO - Query exchange for current position size
    return 0.0
    
def get_order_size():
    # Determine our max allowable position size
    total_position_size = get_position_size()
    position_size = round_to_step(total_position_size / float(config['maxOpenOrders']), state['trade_pair'])
    #log_debug(f"Desired position size = {position_size}"")

    # Determine minimum position size. This works out to be 10 USDT worth of the pair
    # We add a little to this value just to be safe as the price can change quickly
    min_pos_size = get_min_qty(state['trade_pair'])
    #log_debug(f"Minimum position size = {min_pos_size}")
    if position_size < min_pos_size:
        return -1.0

    # Clamp the order size to the max for this market, if applicable
    max_pos_size = get_max_qty(state['trade_pair'])
    if position_size > max_pos_size: # TODO - Break this up into multiple entry / exit orders once this becomes an issue?
        log_debug(f"Desired position size of {position_size} exceeded market max lot size of {max_pos_size} for pair {state['trade_pair']}. Clamping to max lot size")
        position_size = max_pos_size

    return float(position_size)
   
def get_max_qty(pair):
    # Get maxQty for pair
    return float(state['pair_info']['maxposition_size'])

def get_min_qty(pair):
    # Get minQty for pair
    return float(state['pair_info']['minOrderSize'])

def get_tick(pair):
    # Get pair tick size
    return float(state['pair_info']['tick_size'])

def get_step(pair):
    # Get pair step size
    return float(state['pair_info']['step_size'])

def round_to_tick(value, pair):
    # Round passed value to the pair tick size
    tick_size = get_tick(pair)
    tnum_ticks = math.floor(value / tick_size)
    return tnum_ticks * tick_size

def round_to_step(value, pair):
    # Round passed value to the pair step size
    step_size = get_step(pair)
    num_steps = math.floor(value / step_size)
    return num_steps * step_size

def float_to_str(value, pair, precision_type):
    # Convert float to string, the library expects strings for floats
    # and this lets us control the precision of the values passed
    # Precision type can be one of the following:
    #   base_assetPrecision
    #   quote_assetPrecision
    # Make sure the passed value is already rounded using the correct helper function above,
    # otherwise this will do normal rounding up / down based on the size of the fractional part

    # Determine the number of digits past the decimal we are allowed based on the passed precision type
    if precision_type == 'base_assetPrecision':
        size = get_step(pair)
    elif precision_type == 'quote_assetPrecision':
        size = get_tick(pair)
    else:
        # Define a default precision
        log_error(f"Unknown precision value of '{precision_type}' requested in float_to_str(), using 0 as a failsafe")
        size = 1.0

    # Determine precision / number of decimal places from size value
    # This is the brute force method, Might want to do this in a more elegant way
    if size == 1.0:
        precision = 0
    elif size == 0.1:
        precision = 1
    elif size == 0.01:
        precision = 2
    elif size == 0.001:
        precision = 3
    elif size == 0.0001:
        precision = 4
    elif size == 0.00001:
        precision = 5
    elif size == 0.000001:
        precision = 6
    elif size == 0.0000001:
        precision = 7
    elif size == 0.00000001:
        precision = 8
    else:
        log_error(f"Unknown size value of '{size}' requested in float_to_str(), using 1 as a failsafe")
        size = 1.0
        precision = 0
    
    # Generate our format string from the desired precision value
    txt_out = '{val:.' + str(precision) + 'f}'
    
    # Convert the passed value to a string
    txt_out = txt_out.format(val = value)
    
    # Trim any trailing zeros from the resulting string
    while txt_out.__contains__('.') and txt_out[-1] == '0':
        txt_out = txt_out[:-1]
    
    # If all zeros were trimmed, then remove the trailing dot
    if txt_out[-1] == '.':
        txt_out = txt_out[:-1]
    
    return txt_out

def log_add_utc_time(msg):
    # Add the utc time to our log message. UTC is the time used by most exchanges
    # by default, so this will make our troubleshooting easier
    
    # Use the library helper function to get the current ISO time string
    timestamp = dydx3_request_helpers.generate_now_iso()

    # Build output message
    return timestamp + ' - ' + str(msg)

def log_info(msg):
    # Send the the message to the logger with the exchange time stamp added
    logger.info(log_add_utc_time(str(msg)))
    
def log_debug(msg):
    # Send the the message to the logger with the exchange time stamp added
    logger.debug(log_add_utc_time(str(msg)))
    
def log_error(msg):
    # Send the the message to the logger with the exchange time stamp added
    logger.error(log_add_utc_time(str(msg)))

### Core functions ###

def initialize():

    global config
    global state
    global logger
    global dydx_api
    global telegram_api

    try:
                # Initialize our state dict to sane starting values
        state = default_state
        state['bot_run'] = True
        state['bot_init'] = True

        # Wipe our log file before we start to prevent this file from growing infinitely long
        if os.path.isfile(settings['log_file']):
            try:
                os.remove(settings['log_file'])
            except OSError:
                print('Unable to clear out the log file')
                quit()

        # Setup logging
        log_level = logging.DEBUG    # Log level for our console and log file
        logger = logging.getLogger(settings['bot_short_name'])
        logger.setLevel(log_level)
        # Create file handler 
        fh = logging.FileHandler(settings['log_file'])
        fh.setLevel(log_level)
        # Create console handler 
        ch = logging.StreamHandler()
        ch.setLevel(log_level)
        # Create formatter and add it to the handlers
        formatter = one_line_exception_formatter(logging.BASIC_FORMAT)
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        # Add the handlers to the logger
        logger.addHandler(fh)
        logger.addHandler(ch)

        # Redirect stdout and stderr to the logger, this will caputre anything
        # from our modules or the OS in our console and our log file
        sys.stdout = logger_writer(logger, logging.INFO)
        sys.stderr = logger_writer(logger, logging.ERROR)

        # Let the user know we are running
        log_debug(settings['bot_long_name'] + ' ' + settings['bot_version'] + ' initializing')

        # Initialize configuration variables
        if os.path.isfile(settings['config_file']):
            # Load our config from file
            config = json.load(open(settings['config_file']))
            log_debug('Bot config loaded from file')
        else:
            # Use default sane values, if we need other variables, add them here
            config = default_config

            # Save the default config to allow the user to make on the fly changes
            with open(settings['config_file'], 'w') as outfile:
                outfile.write(json.dumps(config, indent=2))
            log_debug('Using default values for bot config')

        # Capture initial enable_trading value and force it to False
        original_enable_trading = config['enable_trading']
        config['enable_trading'] = False

        # Define some connection variables depending on our desired network connection
        if settings['dydx_network'] == 'mainnet':
            api_host = str(dydx3_constants.API_HOST_MAINNET)
            api_network = str(dydx3_constants.NETWORK_ID_MAINNET)
        elif settings['dydx_network'] == 'testnet':
            api_host = str(dydx3_constants.API_HOST_GOERLI)
            api_network = str(dydx3_constants.NETWORK_ID_GOERLI)
        else:
            log_error(f"Setting dydx_network set to unknown value = {settings['dydx_network']} exiting")
            quit()

        # Create a client instance using api key and secret from environment variables
        api_key = str(os.environ.get('DYDX_API_KEY'))
        log_debug(f"Exchange API key = {api_key}")
        if api_key == 'None':
            log_error('DYDX_API_KEY environment variable not found, exiting')
            quit()
        api_secret = str(os.environ.get('DYDX_API_SECRET'))
        if api_secret == 'None':
            log_error('DYDX_API_SECRET environment variable not found, exiting')
            quit()
        api_pass = str(os.environ.get('DYDX_API_PASS'))
        if api_pass == 'None':
            log_error('DYDX_API_PASS environment variable not found, exiting')
            quit()
        api_credentials = {
            'key' : api_key, 
            'secret' : api_secret, 
            'passphrase' : api_pass,
        }
        
        # We need our stark private key for placing orders
        api_stark_key = str(os.environ.get('DYDX_STARK_KEY'))
        if api_stark_key == 'None':
            log_error('DYDX_STARK_KEY environment variable not found, exiting')
            quit()
        
        # ETH address only needed for some top level actions
        # Note that we can also get this from our profile using API credentials
        api_eth_addr = str(os.environ.get('DYDX_ETH_ADDR'))
        if api_eth_addr == 'None':
            log_error('DYDX_ETH_ADDR environment variable not found, exiting')
            quit()
        
        # Create our exchange client
        dydx_api = dydx3_client(
            host=api_host, 
            network_id = api_network, 
            api_key_credentials=api_credentials, 
            stark_private_key=api_stark_key,
            default_ethereum_address=api_eth_addr
        )
        log_debug('DyDx API client created')

        # Capture our position ID so we can place orders
        response = dydx_api.private.get_accounts()
        state['dydx_pos_id'] = response.data['accounts'][0]['positionId']
        log_debug(f"DyDx position ID = {state['dydx_pos_id']}")

        # Build our trade pair name
        pair = settings['base_asset'] + '-' +  settings['quote_asset']
        state['trade_pair'] = pair
        log_debug(f"Trade pair = {pair}")

        # Get the details of this trade pair from the exchange
        response = dydx_api.public.get_markets(pair)
        state['pair_info'] = response.data['markets'][pair]
        log_debug(f"Pair info obtained for pair {pair}")
        
        # If we exited the bot in a position, load our state for the saved file,
        # otherwise we'll start in our default state
        init_state = {}
        init_state['exit_order_id'] = None
        if os.path.isfile(settings['state_file']):
            init_state = json.load(open(settings['state_file']))
            try:
                os.remove(settings['state_file'])
            except OSError:
                log_error('Unable to clear out the state file')
        if not init_state['exit_order_id'] == None:
            state = json.load(open(settings['state_file']))
            log_debug(f"Found open position for pair {state['trade_pair']}, loading state from file")
        else:
            # We've already set our state to defaults earlier on, so just need to initialize a few more values here
            state['trade_pair'] = pair
            update_current_balance()
            state['starting_balance'] = state['current_total_balance']
            log_debug(f"No open position found for pair {pair}, using default state")

        # Read telegram API variables from our environment
        app_id = str(os.environ.get('TELEGRAM_APP_ID'))
        if app_id == 'None':
            log_error('TELEGRAM_APP_ID environment variable not found, exiting')
            quit()
        
        app_hash = str(os.environ.get('TELEGRAM_APP_HASH'))
        if app_hash == 'None':
            log_error('TELEGRAM_APP_HASH environment variable not found, exiting')
            quit()
            
        # Setup the telegram bot
        telegram_api = TelegramClient(settings['bot_short_name'], app_id, app_hash)

        # Restore our enable_trading value
        config['enable_trading'] = original_enable_trading

        # Let the usr know we completed initialization
        log_info(f"{settings['bot_long_name']} {settings['bot_version']} started, waiting for user messages")

    except Exception as e:
        log_error('Exception occured in initialize')
        log_error(e)
        state['bot_run'] == False

def parse_trade_message(msg_text):

    # Parses the chat message and returns the requested order details
    # Will return None if it can't parse the passed message

    # Do some formating on the passed message
    msg = str(msg_text).strip().upper()

    # Make sure our message has our seperation character
    if not ',' in msg:
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Message does not include comma seperators, ignoring')
        return None
    
    # Split our message into its parts
    msg_parts = str(msg).split(sep=',')

    # Make sure we got the right number of parts
    if not len(msg_parts) == 3:
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Message does not have the right number of parts, ignoring')
        return None

    # Extract our message parts
    msg_timestamp = str(msg_parts[0]).strip()
    msg_buy_order = str(msg_parts[1]).strip()
    msg_sell_order = str(msg_parts[2]).strip()

    # Make sure our buy order starts with BUY
    if not msg_buy_order.startswith('BUY'):
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part does not start with BUY, ignoring')
        return None

    # Make sure our buy order contains our part seperator
    if not msg_buy_order.contains(' '):
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part does not include multiple parts, ignoring')
        return None

    buy_parts = msg_buy_order.split(' ')
    
    # Make sure our buy order has the right number of parts
    if not len(buy_parts) == 5:
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part does not have the right number of parts, ignoring')
        return None

    # Extract our buy parts
    msg_buy_command = str(buy_parts[0]).strip()
    msg_buy_pair = str(buy_parts[1]).strip()
    msg_buy_count = str(buy_parts[2]).strip()
    msg_buy_of = str(buy_parts[3]).strip()
    msg_buy_total = str(buy_parts[4]).strip()
    
    # Make sure we start with the command
    if not msg_buy_command == 'BUY':
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part does not start with BUY, ignoring')
        return None

    # Make sure we are dealing with a supported pair
    if not msg_buy_pair == 'BTC_USD':
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part does not include seported pair, ignoring')
        return None

    # Make sure the part between our numbers is the right one
    if not msg_buy_of == 'OF':
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part number seperator incorrect, ignoring')
        return None

    # Make sure we got order numbers
    if not msg_buy_count.isnumeric() and not msg_buy_total.isnumeric():
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part count or total are not numbers, ignoring')
        return None

    # Make sure our count is less than or equal to our total
    if msg_buy_count > msg_buy_total:
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Second part count is greater than total, ignoring')
        return None

    # Make sure our sell order starts with SELL
    if not msg_sell_order.startswith('SELL'):
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part does not start with SELL, ignoring')
        return None
    
    # Make sure our sell order contains our part seperator
    if not msg_sell_order.contains(' '):
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part does not include multiple parts, ignoring')
        return None

    sell_parts = msg_buy_order.split(' ')
    
    # Make sure our sell order has the right number of parts
    if not len(sell_parts) == 3:
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part does not have the right number of parts, ignoring')
        return None

    # Extract our buy parts
    msg_sell_command = str(sell_parts[0]).strip()
    msg_sell_at = str(sell_parts[1]).strip()
    msg_sell_price = str(sell_parts[2]).strip()
    
    # Make sure we start with the command
    if not msg_sell_command == 'SELL':
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part does not start with SELL, ignoring')
        return None

    # Make sure the part between our command and price is the right one
    if not msg_sell_at == '@':
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part seperator incorrect, ignoring')
        return None

    # Make sure we our price value
    if not msg_sell_price.isnumeric():
        log_debug(f"Failed to parse trade message : {msg}")
        log_debug('Third part price is not a number, ignoring')
        return None

    # Build up our response
    order = {
        'pair' : msg_buy_pair,
        'buy_count' : msg_buy_count,
        'buy_total' : msg_buy_total,
        'sell_price' : msg_sell_price,
    }

    return order

def process_order(order):

    # Processes a buy / sell order request
    
    # TODO - Implement this...

    pass

### Main loop ###

try:
    # Initialize our bot
    initialize()
    
    # Unit tests
    log_debug("Running unit tests")
    parse_trade_message(' Test ')
    parse_trade_message('1,2')
    parse_trade_message('1, 2, 3')
    parse_trade_message('1,BUY,EXIT')
    parse_trade_message('2022-11-22T14:00:00Z, BUY BTC-USD 5 of 8, SELL @ 18698')
    state['bot_run'] = False

    # Register our new message handler
    @telegram_api.on(NewMessage)
    async def my_event_handler(event):
        # Extrace message details
        msg_text = event.raw_text
        msg_chat_id = event.chat_id
        msg_sender_id = event.sender_id
        
        # Handle messages that we are interested in
        if msg_chat_id == settings['chat_id'] and msg_sender_id == settings['bot_id']:
            order = parse_trade_message(msg_text)
            if not order == None:
                process_order(order)

    # Loop until we are done
    while(state['bot_run']):
        telegram_api.start()
        telegram_api.run_until_disconnected()
        log_debug('Detected telegram_api disconnect - restarting')
        
except KeyboardInterrupt:
    print()
    log_debug('User requested bot stop via CTRL+C')
    state['bot_run'] = False
finally:
    # Save our current state to a file if we are in a position 
    # so we can pick back up where we left off if we are re-started
    if not state['exit_order_id'] == None:
        with open(settings['state_file'], 'w') as outfile:
            outfile.write(json.dumps(state, indent=2))
        log_debug('State information saved to file')

    log_info('All done, bot terminating')
