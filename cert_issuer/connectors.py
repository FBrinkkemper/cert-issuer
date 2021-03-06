"""
Connectors wrap the details of communicating with different Bitcoin clients and implementations.
"""
import io
import logging
import time
from abc import abstractmethod

import bitcoin.rpc
import requests
from bitcoin.core import CTransaction
from cert_schema import Chain
from pycoin.serialize import b2h, b2h_rev, h2b
from pycoin.services import providers
from pycoin.services.chain_so import ChainSoProvider
from pycoin.services.insight import InsightProvider
from pycoin.services.providers import service_provider_methods
from pycoin.tx.Spendable import Spendable

from cert_issuer import helpers
from cert_issuer.errors import ConnectorError, BroadcastError

BROADCAST_RETRY_INTERVAL = 30

try:
    from urllib2 import urlopen, HTTPError
    from urllib import urlencode
except ImportError:
    from urllib.request import urlopen, HTTPError
    from urllib.parse import urlencode

MAX_BROADCAST_ATTEMPTS = 3


def try_get(url):
    """throw error if call fails"""
    response = requests.get(url)
    if int(response.status_code) != 200:
        error_message = 'Error! status_code={}, error={}'.format(
            response.status_code, response.json()['error'])
        logging.error(error_message)
        raise ConnectorError(error_message)
    return response


def to_hex(transaction):
    s = io.BytesIO()
    transaction.stream(s)
    tx_as_hex = b2h(s.getvalue())
    return tx_as_hex

class EtherscanBroadcaster(object):
    def __init__(self, base_url):
        self.base_url = base_url
    
    def broadcast_tx(self, tx, api_token):
        tx_hex = tx

        broadcast_url = self.base_url + '?module=proxy&action=eth_sendRawTransaction'
        if api_token:
            '&apikey=%s' % api_token
        response = requests.post(broadcast_url, data={'hex': tx_hex})
        if int(response.status_code) == 200:
            tx_id = response.json().get('result', None)
            logging.info("Transaction ID obtained from broadcast through Etherscan: %s", tx_id)
            return tx_id
        logging.error('Error broadcasting the transaction through the Etherscan API. Error msg: %s', response.text)
        raise BroadcastError(response.text)
    
    def get_balance(self, address, api_token):
        """
        returns the balance in wei
        with some inspiration from PyWallet
        """
        broadcast_url = self.base_url + '?module=account&action=balance' 
        broadcast_url += '&address=%s' % address
        broadcast_url += '&tag=latest'
        if api_token:
            '&apikey=%s' % api_token
        response = requests.get(broadcast_url)
        if int(response.status_code) ==  200:
            balance = int(response.json().get('result', None))
            logging.info('Balance check went correct: %s', response.json())
            return balance
        raise BroadcastError(response.text)
    
    def get_address_nonce(self, address, api_token):
        """
        Looks up the address nonce of this address
        Neccesary for the transaction creation
        """
        broadcast_url = self.base_url + '?module=proxy&action=eth_getTransactionCount'
        broadcast_url += '&address=%s' % address
        broadcast_url += '&tag=latest'
        if api_token:
            '&apikey=%s' % api_token
        response = requests.get(broadcast_url)
        if int(response.status_code) == 200:
            #the int(res, 0) transforms the hex nonce to int
            nonce = int(response.json().get('result', None), 0)
            logging.info('Nonce check went correct: %s', response.json())
            return nonce
        else:
            logging.info('response error checking nonce')
        raise BroadcastError('Error checking the nonce through the Etherscan API. Error msg: %s', response.text)
        
        
class BlockExplorerBroadcaster(object):
    def __init__(self, base_url):
        self.base_url = base_url

    def broadcast_tx(self, tx):
        hextx = to_hex(tx)
        broadcast_url = self.base_url + '/tx/send'
        response = requests.post(broadcast_url, json={'rawtx': hextx})
        if int(response.status_code) == 200:
            tx_id = response.json().get('txid', None)
            return tx_id
        logging.error('Error broadcasting the transaction through the BlockExplorer API. Error msg: %s', response.text)
        raise BroadcastError(response.text)


class BlockcypherBroadcaster(object):
    """
    Note that this needs an API token
    """

    def __init__(self, base_url, api_token):
        self.base_url = base_url
        self.api_token = api_token

    def broadcast_tx(self, tx):
        hextx = to_hex(tx)
        broadcast_url = self.base_url + '/txs/push?token=' + self.api_token
        response = requests.post(broadcast_url, json={'tx': hextx})
        if int(response.status_code) == 200:
            tx_id = response.json().get('txid', None)
            return tx_id
        logging.error('Error broadcasting the transaction through the Blockcypher API. Error msg: %s', response.text)
        raise BroadcastError(response.text)


class BlockrIOBroadcaster(object):
    def __init__(self, base_url):
        self.base_url = base_url

    def broadcast_tx(self, tx):
        hextx = to_hex(tx)
        url = self.base_url + '/tx/push'
        response = requests.post(url, json={'hex': hextx})
        if int(response.status_code) == 200:
            tx_id = response.json().get('data', None)
            return tx_id
        logging.error('Error broadcasting the transaction through the Blockr.IO API. Error msg: %s', response.text)
        raise BroadcastError(response.text)


class BitcoindConnector(object):
    def __init__(self, netcode):
        self.netcode = netcode

    def broadcast_tx(self, transaction):
        as_hex = transaction.as_hex()
        transaction = CTransaction.deserialize(h2b(as_hex))
        tx_id = bitcoin.rpc.Proxy().sendrawtransaction(transaction)
        # reverse endianness for bitcoind
        return b2h_rev(tx_id)

    def spendables_for_address(self, address):
        """
        Converts to pycoin Spendable type
        :param address:
        :return: list of Spendables
        """
        unspent_outputs = bitcoin.rpc.Proxy().listunspent(addrs=[address])
        logging.debug('spendables_for_address %s', address)

        spendables = []
        for unspent in unspent_outputs:
            coin_value = unspent.get('amount', 0)
            outpoint = unspent.get('outpoint')
            script = unspent.get('scriptPubKey')
            previous_hash = outpoint.hash
            previous_index = outpoint.n
            spendables.append(Spendable(coin_value, script, previous_hash, previous_index))
        return spendables


class ServiceProviderConnector(object):
    @abstractmethod
    def get_balance(self, address):
        pass

    def broadcast_tx(self, tx):
        pass

class EthereumServiceProviderConnector(ServiceProviderConnector):
    #param local_node indicates if a local node is running or if the tx should be broadcast to external providers 
    def __init__(self, ethereum_chain, api_key, local_node=False):
        self.ethereum_chain = ethereum_chain
        self.api_key = api_key
        self.local_node = local_node
    
    def get_balance(self, address):
        for m in get_providers_for_chain(self.ethereum_chain, self.local_node):
            if isinstance(m, BitcoindConnector):
                pass
            else:    
                try:
                    logging.debug('m=%s', m)
                    balance = m.get_balance(address, self.api_key)
                    return balance
                except Exception as e:
                    logging.warning(e)
                    pass            
        return 0

    def get_address_nonce(self, address):
        for m in get_providers_for_chain(self.ethereum_chain, self.local_node):
            try:
                logging.debug('m=%s', m)
                nonce = m.get_address_nonce(address, self.api_key)
                return nonce
            except Exception as e:
                logging.warning(e)
                pass
        return 0

    def broadcast_tx(self, tx):
        for m in get_providers_for_chain(self.ethereum_chain, self.local_node):
            try:
                logging.debug('m=%s', m)
                txid = m.broadcast_tx(tx, self.api_key)
                return txid
            except Exception as e:
                logging.warning(e)
                pass
        ##in case of failure:
        return '0xfail'
         

class BitcoinServiceProviderConnector(ServiceProviderConnector):
    def __init__(self, bitcoin_chain, bitcoind=False):
        self.bitcoin_chain = bitcoin_chain
        self.bitcoind = bitcoind

    def spendables_for_address(self, bitcoin_address):
        for m in service_provider_methods('spendables_for_address', get_providers_for_chain(self.bitcoin_chain, self.bitcoind)):
            try:
                logging.debug('m=%s', m)
                spendables = m(bitcoin_address)
                return spendables
            except Exception as e:
                logging.warning(e)
                pass
        return []

    def get_unspent_outputs(self, address):
        """
        Get unspent outputs at the address
        :param address:
        :return:
        """
        logging.debug('get_unspent_outputs for address=%s', address)
        spendables = self.spendables_for_address(bitcoin_address=address)
        if spendables:
            return sorted(spendables, key=lambda x: hash(x.coin_value))
        return None

    def get_balance(self, address):
        """
        Get balance available to spend at the address
        :param address:
        :return:
        """
        spendables = self.get_unspent_outputs(address)
        if not spendables:
            logging.warning('address %s has a balance of 0', address)
            return 0

        balance = sum(s.coin_value for s in spendables)
        return balance

    def broadcast_tx(self, tx):
        """
        Broadcast the transaction through the configured set of providers

        :param tx:
        :return:
        """
        return BitcoinServiceProviderConnector.broadcast_tx_with_chain(tx, self.bitcoin_chain, self.bitcoind)

    @staticmethod
    def broadcast_tx_with_chain(tx, bitcoin_chain, bitcoind=False):
        """
        Broadcast the transaction through the configured set of providers

        :param tx:
        :param bitcoin_chain:
        :return:
        """
        last_exception = None
        final_tx_id = None

        # Unlike other providers, we want to broadcast to all available apis
        for attempt_number in range(0, MAX_BROADCAST_ATTEMPTS):
            for method_provider in service_provider_methods('broadcast_tx',
                                                            get_providers_for_chain(bitcoin_chain, bitcoind)):
                try:
                    tx_id = method_provider(tx)
                    if tx_id:
                        logging.info('Broadcasting succeeded with method_provider=%s, txid=%s', str(method_provider),
                                     tx_id)
                        if final_tx_id and final_tx_id != tx_id:
                            logging.error(
                                'This should never happen; fail and investigate if it does. Got conflicting tx_ids=%s and %s. Hextx=%s',
                                final_tx_id, tx_id, tx.as_hex())
                            raise Exception('Got conflicting tx_ids.')
                        final_tx_id = tx_id
                except Exception as e:
                    logging.warning('Caught exception trying provider %s. Trying another. Exception=%s',
                                    str(method_provider), e)
                    last_exception = e
            # At least 1 provider succeeded, so return
            if final_tx_id:
                return final_tx_id
            else:
                logging.warning('Broadcasting failed. Waiting before retrying. This is attempt number %d',
                                attempt_number)
                time.sleep(BROADCAST_RETRY_INTERVAL)
        logging.error('Failed broadcasting through all providers')
        logging.error(last_exception, exc_info=True)
        raise BroadcastError(last_exception)


PYCOIN_BTC_PROVIDERS = "blockchain.info blockexplorer.com blockcypher.com chain.so"
PYCOIN_XTN_PROVIDERS = "blockexplorer.com"  # chain.so

# initialize connectors
connectors = {}


# configure mainnet providers
provider_list = providers.providers_for_config_string(PYCOIN_BTC_PROVIDERS, helpers.to_pycoin_chain(Chain.bitcoin_mainnet))
provider_list.append(BlockrIOBroadcaster('https://btc.blockr.io/api/v1'))
provider_list.append(BlockExplorerBroadcaster('https://blockexplorer.com/api'))
provider_list.append(InsightProvider(netcode=helpers.to_pycoin_chain(Chain.bitcoin_mainnet)))
provider_list.append(ChainSoProvider(netcode=helpers.to_pycoin_chain(Chain.bitcoin_mainnet)))
connectors[Chain.bitcoin_mainnet] = provider_list


# configure testnet providers
xtn_provider_list = providers.providers_for_config_string(PYCOIN_XTN_PROVIDERS, helpers.to_pycoin_chain(Chain.bitcoin_testnet))
xtn_provider_list.append(ChainSoProvider(netcode=helpers.to_pycoin_chain(Chain.bitcoin_testnet)))
xtn_provider_list.append(BlockrIOBroadcaster('https://tbtc.blockr.io/api/v1'))
xtn_provider_list.append(BlockExplorerBroadcaster('https://testnet.blockexplorer.com/api'))
connectors[Chain.bitcoin_testnet] = xtn_provider_list

#Configure Ethereum mainnet connectors 
eth_provider_list = []
eth_provider_list.append(EtherscanBroadcaster('https://api.etherscan.io/api'))
connectors[Chain.ethereum_mainnet] = eth_provider_list

#Configure Ethereum Ropsten testnet connectors
rop_provider_list = []
rop_provider_list.append(EtherscanBroadcaster('https://ropsten.etherscan.io/api'))
connectors[Chain.ethereum_ropsten] = rop_provider_list


def get_providers_for_chain(chain, bitcoind=False):
    if bitcoind:
        return [BitcoindConnector(helpers.to_pycoin_chain(chain))]
    else:
        return connectors[chain]

