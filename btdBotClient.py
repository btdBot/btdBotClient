# Copyright 2022 Sandy Pyke
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
from telethon.errors import SessionPasswordNeededError

from dydx3 import Client as dydx3_client
from dydx3 import constants as dydx3_constants
from dydx3.helpers import request_helpers as dydx3_request_helpers

### Global variables ###

config = {}
state = {}
logger = None
dydxApi = None

### Global bot settings ###

settings = {

    # Bot identification
    'botVersion' : 'V0.1',
    'botShortName' : 'btdBotClient',
    'botLongName' : 'Buy The Dip BOT client for dYdX',

    # This asset will be monitored for trding signals, it will be combined with
    # the quote asset to build the pair name. This algorithm will be in position
    # most of the time, so this is not compatible with trading multiple assets in
    # parallel
    'baseAsset' : 'BTC',

    # Our base currency against which the above assets will be measured
    'quoteAsset' : 'USD',

    # Connect to dydx's mainnet or testnetss
    # Make sure environment variables match selected network...
    'dydxNetwork' : 'mainnet',
    #'dydxNetwork' : 'testnet',
}

### File names ###

# File where log output will be saved
settings['logFile'] = settings['botShortName'] + '.log'      # Name of our log file

# Our config file, we store varibles here that the user can adjust on the fly to avoid
# having to restart the bot
settings['configFile'] = settings['botShortName'] + '_config.json'

# File where bot state is saved on exit
settings['stateFile'] = settings['botShortName'] + '_state.json'

# The bot will store the telegram chat id in this file, so the user does not need to resend
# a start command everytime the bot is re-started
settings['telegramChatIdfile'] = settings['botShortName'] + '_chatId.json'

# Dump file for dumping the internal dataframes for all trade pairs so we can see what the
# bot is thinking. Don't include the extention as we may dump in a few different formats.
settings['dumpFile'] = settings['botShortName'] + '_dump'

### Bot defaults ###

# Default config values
defaultConfig = {
    # Use this to disable trading when we need to test something
    'enableTrading' : True,

    # Percent of balance to use for trading with 1.0 = 100%
    # Set this to 1.0 or less to control how much equity the bot should use
    'tradeSizeFactor' : 0.95,
}

# Default state variables
defaultState = {
    'botRun' : True,
    'botInit' : True,
    'currentTotalBalance' : -1.0,
    'currentQuoteBalance' : -1.0,
    'startingBalance' : -1.0,
    'tradePair' : '',
    'pairInfo' : {},
    'openPosInfo' : [],
    'openTrades' : 0,
    'exitOrderID' : None,
    'dydxPosId' : 0,
}

### Helper classes ###

class loggerWriter:
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

class OneLineExceptionFormatter(logging.Formatter):
    # Class to handle formating multi-line exceptions into single lines for logs
    def formatException(self, exc_info):
        result = super().formatException(exc_info)
        return repr(result)
 
    def format(self, record):
        result = super().format(record)
        if record.exc_text:
            result = result.replace("\n", "")
        return result

### Helper functions ###

def update_current_balance():
    global state

    response = dydxApi.private.get_accounts()
    account = response.data['accounts'][0]
    state['currentTotalBalance'] = float(account['equity'])
    log_debug(f"Current balance = {state['currentTotalBalance']} {settings['quoteAsset']}")

def get_position_size():
    # TODO - Query exchange for current position size
    return 0.0
    
def get_order_size():
    # Determine our max allowable position size
    totalPositionSize = get_position_size()
    positionSize = round_to_step(totalPositionSize / float(config['maxOpenOrders']), state['tradePair'])
    #log_debug(f"Desired position size = {positionSize}"")

    # Determine minimum position size. This works out to be 10 USDT worth of the pair
    # We add a little to this value just to be safe as the price can change quickly
    minPosSize = get_minQty(state['tradePair'])
    #log_debug(f"Minimum position size = {minPosSize}")
    if positionSize < minPosSize:
        return -1.0

    # Clamp the order size to the max for this market, if applicable
    maxPosSize = get_maxQty(state['tradePair'])
    if positionSize > maxPosSize: # TODO - Break this up into multiple entry / exit orders once this becomes an issue?
        log_debug(f"Desired position size of {positionSize} exceeded market max lot size of {maxPosSize} for pair {state['tradePair']}. Clamping to max lot size")
        positionSize = maxPosSize

    return float(positionSize)
   
def get_maxQty(pair):
    # Get maxQty for pair
    return float(state['pairInfo']['maxPositionSize'])

def get_minQty(pair):
    # Get minQty for pair
    return float(state['pairInfo']['minOrderSize'])

def get_tick(pair):
    # Get pair tick size
    return float(state['pairInfo']['tickSize'])

def get_step(pair):
    # Get pair step size
    return float(state['pairInfo']['stepSize'])

def round_to_tick(value, pair):
    # Round passed value to the pair tick size
    tickSize = get_tick(pair)
    numTicks = math.floor(value / tickSize)
    return numTicks * tickSize

def round_to_step(value, pair):
    # Round passed value to the pair step size
    stepSize = get_step(pair)
    numSteps = math.floor(value / stepSize)
    return numSteps * stepSize

def float_to_str(value, pair, precisionType):
    # Convert float to string, the library expects strings for floats
    # and this lets us control the precision of the values passed
    # Precision type can be one of the following:
    #   baseAssetPrecision
    #   quoteAssetPrecision
    # Make sure the passed value is already rounded using the correct helper function above,
    # otherwise this will do normal rounding up / down based on the size of the fractional part

    # Determine the number of digits past the decimal we are allowed based on the passed precision type
    if precisionType == 'baseAssetPrecision':
        size = get_step(pair)
    elif precisionType == 'quoteAssetPrecision':
        size = get_tick(pair)
    else:
        # Define a default precision
        log_error(f"Unknown precision value of '{precisionType}' requested in float_to_str(), using 0 as a failsafe")
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
    txtOut = '{val:.' + str(precision) + 'f}'
    
    # Convert the passed value to a string
    txtOut = txtOut.format(val = value)
    
    # Trim any trailing zeros from the resulting string
    while txtOut.__contains__('.') and txtOut[-1] == '0':
        txtOut = txtOut[:-1]
    
    # If all zeros were trimmed, then remove the trailing dot
    if txtOut[-1] == '.':
        txtOut = txtOut[:-1]
    
    return txtOut

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
    global dydxApi

    try:
                # Initialize our state dict to sane starting values
        state = defaultState
        state['botRun'] = True
        state['botInit'] = True

        # Wipe our log file before we start to prevent this file from growing infinitely long
        if os.path.isfile(settings['logFile']):
            try:
                os.remove(settings['logFile'])
            except OSError:
                print('Unable to clear out the log file')
                quit()

        # Setup logging
        logLevel = logging.DEBUG    # Log level for our console and log file
        logger = logging.getLogger(settings['botShortName'])
        logger.setLevel(logLevel)
        # Create file handler 
        fh = logging.FileHandler(settings['logFile'])
        fh.setLevel(logLevel)
        # Create console handler 
        ch = logging.StreamHandler()
        ch.setLevel(logLevel)
        # Create formatter and add it to the handlers
        formatter = OneLineExceptionFormatter(logging.BASIC_FORMAT)
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        # Add the handlers to the logger
        logger.addHandler(fh)
        logger.addHandler(ch)

        # Redirect stdout and stderr to the logger, this will caputre anything
        # from our modules or the OS in our console and our log file
        sys.stdout = loggerWriter(logger, logging.INFO)
        sys.stderr = loggerWriter(logger, logging.ERROR)

        # Let the user know we are running
        log_debug(settings['botLongName'] + ' ' + settings['botVersion'] + ' initializing')

        # Initialize configuration variables
        if os.path.isfile(settings['configFile']):
            # Load our config from file
            config = json.load(open(settings['configFile']))
            log_debug('Bot config loaded from file')
        else:
            # Use default sane values, if we need other variables, add them here
            config = defaultConfig

            # Save the default config to allow the user to make on the fly changes
            with open(settings['configFile'], 'w') as outfile:
                outfile.write(json.dumps(config, indent=2))
            log_debug('Using default values for bot config')

        # Capture initial enableTrading value and force it to False
        # This will allow us to run our historical data through our bot logic without triggering trades
        originalEnableTrading = config['enableTrading']
        config['enableTrading'] = False

        # Define some connection variables depending on our desired network connection
        if settings['dydxNetwork'] == 'mainnet':
            apiHost = str(dydx3_constants.API_HOST_MAINNET)
            apiNetwork = str(dydx3_constants.NETWORK_ID_MAINNET)
        elif settings['dydxNetwork'] == 'testnet':
            apiHost = str(dydx3_constants.API_HOST_GOERLI)
            apiNetwork = str(dydx3_constants.NETWORK_ID_GOERLI)
        else:
            log_error(f"Unknown setting dydxNetork = {settings['dydxNetwork']} exiting")
            quit()

        # Create a client instance using api key and secret from environment variables
        apiKey = str(os.environ.get('dydx_api_key'))
        log_debug(f"Exchange API key = {apiKey}")
        if apiKey == 'None':
            log_error('dydx_api_key environment variable not found, exiting')
            quit()
        apiSecret = str(os.environ.get('dydx_api_secret'))
        if apiSecret == 'None':
            log_error('dydx_api_secret environment variable not found, exiting')
            quit()
        apiPass = str(os.environ.get('dydx_api_pass'))
        if apiPass == 'None':
            log_error('dydx_api_pass environment variable not found, exiting')
            quit()
        api_credentials = {
            'key' : apiKey, 
            'secret' : apiSecret, 
            'passphrase' : apiPass,
        }
        
        # We need our stark private key for placing orders
        apiStarkKey = str(os.environ.get('dydx_stark_key'))
        if apiStarkKey == 'None':
            log_error('dydx_stark_key environment variable not found, exiting')
            quit()
        
        # ETH address only needed for some top level actions
        # Note that we can get this from our profile using API credentials only
        apiEthAddr = str(os.environ.get('dydx_eth_addr'))
        if apiEthAddr == 'None':
            log_error('dydx_eth_addr environment variable not found, exiting')
            quit()
        
        dydxApi = dydx3_client(
            host=apiHost, 
            network_id = apiNetwork, 
            api_key_credentials=api_credentials, 
            stark_private_key=apiStarkKey,
            default_ethereum_address=apiEthAddr
        )
        log_debug('DyDx API client created')

        # Capture our position ID so we can place orders
        response = dydxApi.private.get_accounts()
        state['dydxPosId'] = response.data['accounts'][0]['positionId']
        log_debug(f"DyDx position ID = {state['dydxPosId']}")

        # Build our trade pair name
        pair = settings['baseAsset'] + '-' +  settings['quoteAsset']
        state['tradePair'] = pair
        log_debug(f"Trade pair = {pair}")

        # Get the details of this trade pair from the exchange
        response = dydxApi.public.get_markets(pair)
        state['pairInfo'] = response.data['markets'][pair]
        log_debug(f"Pair info obtained for pair {pair}")
        
        # If we exited the bot in a position, load our state for the saved file,
        # otherwise we'll start in our default state
        initState = {}
        initState['exitOrderID'] = None
        if os.path.isfile(settings['stateFile']):
            initState = json.load(open(settings['stateFile']))
            try:
                os.remove(settings['stateFile'])
            except OSError:
                log_error('Unable to clear out the state file')
        if not initState['exitOrderID'] == None:
            state = json.load(open(settings['stateFile']))
            log_debug(f"Found open position for pair {state['tradePair']}, loading state from file")
        else:
            # We've already set our state to defaults earlier on, so just need to initialize a few more values here
            state['tradePair'] = pair
            update_current_balance()
            state['startingBalance'] = state['currentTotalBalance']
            log_debug(f"No open position found for pair {pair}, using default state")

        # Setup the telegram bot
        # TODO - Implement this...

    except Exception as e:
        log_error('Exception occured in initialize')
        log_error(e)
        state['botRun'] == False

### Main loop ###

try:
    # Initialize our bot
    initialize()
    
    # Unit tests
    #log_debug("Running unit tests")
    #state['botRun'] = False

    # Loop until we are done
    while(state['botRun']):
        time.sleep(1)
        
except KeyboardInterrupt:
    print()
    log_debug('User requested bot stop via CTRL+C')
    state['botRun'] = False
finally:
    # Save our current state to a file if we are in a position 
    # so we can pick back up where we left off if we are re-started
    if not state['exitOrderID'] == None:
        with open(settings['stateFile'], 'w') as outfile:
            outfile.write(json.dumps(state, indent=2))
        log_debug('State information saved to file')

    log_info('All done, bot terminating')