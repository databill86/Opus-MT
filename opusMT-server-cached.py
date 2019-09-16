#!/usr/bin/python3
#-*-python-*-
#
#

from __future__ import print_function, unicode_literals, division

import time
import signal
import sys
import argparse
import codecs
import json
# import socketserver
from SimpleWebSocketServer import SimpleWebSocketServer, WebSocket
from websocket import create_connection


## pre-processing: 
## - moses-script wrapper classes
## - BPE-based segmentation from subword-nmt
from apply_bpe import BPE
from mosestokenizer import *

## language identifer (if source language is not given)
import pycld2 as cld2

## for the cache
## --> unlimited size
## --> assumes that translations from the server don't change
from sqlitedict import SqliteDict

parser = argparse.ArgumentParser(description='Simple translation server.')
parser.add_argument('-p','--port', type=int, default=8080,
                   help='socket the server will listen on')
parser.add_argument('-mth','--mthost','--mt-server-host', type=str, default='localhost',
                   help='host of the MT server')
parser.add_argument('-mtp','--mtport','--mt-server-port', type=int, default=11111,
                   help='port of the MT server')
parser.add_argument('-s','--srclangs', metavar='srclangs', type=str, nargs='+',
                    default=['de','fr','sv','en'],
                    help='supported source languages')
parser.add_argument('-t','--trglangs', metavar='trglangs',type=str, nargs='+',
                    default=['fi','et','hu'],
                    help='supported source languages')
parser.add_argument('-d','--deftrg','--default-target-language', type=str,
                    help='default target language (for multilingual models)')
parser.add_argument('--bpe','--bpe-model', type=str, default='opus.de+fr+sv+en.bpe32k-model',
                    help='BPE model for source text segmentation')
parser.add_argument('-c','--cache', type=str, default='opentrans-cache.db',
                    help='BPE model for source text segmentation')


args = parser.parse_args()


if not args.deftrg: 
    args.deftrg = args.trglangs[0]



## BPE model for pre-processing
print("load BPE codes from " + args.bpe, flush=True)
BPEcodes = codecs.open(args.bpe, encoding='utf-8')
bpe = BPE(BPEcodes)


## open the cache DB
print("open cache at " + args.cache, flush=True)
cache = SqliteDict(args.cache, autocommit=True)


## add signal handler for SIGINT to properly close 
## the DB when interrupting
def signal_handler(sig, frame):
    cache.close()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


## pre- and post-processing tools
tokenizer = {}
sentence_splitter = {}
normalizer = {}
detokenizer = {}

print("start pre- and post-processing tools")
for l in args.srclangs:
    sentence_splitter[l] = MosesSentenceSplitter(l)
    normalizer[l] = MosesPunctuationNormalizer(l)
    tokenizer[l] = MosesTokenizer(l)

for l in args.trglangs:
    detokenizer[l] = MosesDetokenizer(l)


# open connection
print("open connection to " + args.mthost + " at port " + str(args.mtport), flush=True)
ws = create_connection("ws://{}:{}/translate".format(args.mthost, args.mtport))


## when using TCP Socket Server
# class Translate(socketserver.BaseRequestHandler):
#    def handle(self):
#        self.data = self.request.recv(1024).strip().decode('utf-8')
#        
#        print("{} wrote:".format(self.client_address[0]), flush=True)
#        print(self.data, flush=True)


class Translate(WebSocket):

    def handleMessage(self):

        prefix = ''
        fromLang = None
        toLang = args.deftrg
        # print('received: ' + self.data, flush=True)

        ## check whether we retrieve a JSON string or not
        try:
            data = json.loads(self.data)
            srctxt = data['text']
            if 'source' in data:
                if data['source'] != 'detect':
                    fromLang = data['source']
            if 'target' in data:
                if data['target']:
                    toLang = data['target']
        except ValueError as error:
            print("invalid json: %s" % error)
            srctxt = self.data
            # check whether first token indiciates language pair
            tokens = srctxt.split()
            langs = tokens.pop(0).split('-')
            if len(langs) == 2:
                toLang = langs[1]
                if langs[0] != 'DL' and langs[0] != 'detect':
                    fromLang = langs[0]
                    srctxt = " ".join(tokens)

        ## check the cache first
        # print('input: ' + srctxt, flush=True)
        if srctxt in cache:
            translation = cache[srctxt]
            print('CACHED TRANSLATION: ' + translation, flush=True)
            data = {'result': translation, 'origin': 'cache'}
            self.sendMessage(json.dumps(data, sort_keys=True, indent=4))
            # self.request.sendall(bytes(json.dumps(data, sort_keys=True, indent=4), "utf-8"))
            # self.request.sendall(bytes(translation, "utf-8"))
            return

        if len(args.trglangs) > 1:
            prefix = '>>' + toLang + '<< '

        if not fromLang:
            isReliable, textBytesFound, details = cld2.detect(srctxt, bestEffort=True)
            fromLang = details[0][1]
            print("language detected = " + fromLang, flush=True)

        if not fromLang in args.srclangs:
            print('unsupported source language ' + fromLang, flush=True)
            data = {'error': 'unsupported source language ' + fromLang,
                    'source': fromLang, 'target': toLang}
            self.sendMessage(json.dumps(data, sort_keys=True, indent=4))
            # self.request.sendall(bytes(json.dumps(data, sort_keys=True, indent=4), "utf-8"))
            # self.request.sendall(bytes('ERROR: unsupported source language ' + fromLang, "utf-8"))
            return

        if not toLang in args.trglangs:
            print('unsupported target language ' + toLang, flush=True)
            data = {'error': 'unsupported target language ' + toLang,
                    'source': fromLang, 'target': toLang}
            self.sendMessage(json.dumps(data, sort_keys=True, indent=4))
            # self.request.sendall(bytes(json.dumps(data, sort_keys=True, indent=4), "utf-8"))
            # self.request.sendall(bytes('ERROR: unsupported target language ' + toLang, "utf-8"))
            return

        langpair = fromLang + toLang
        message = []
        for s in sentence_splitter[fromLang]([normalizer[fromLang](srctxt)]):
            key = langpair + ' ' + s
            if key in cache:
                detokenized = cache[key]
                print('CACHED TRANSLATION: ' + detokenized, flush=True)
            else:
                # print('raw sentence: ' + s, flush=True)
                tokenized = ' '.join(tokenizer[fromLang](s))
                # print('tokenized sentence: ' + tokenized, flush=True)
                segmented = bpe.process_line(tokenized)
                # print('segmented sentence ' + prefix + segmented, flush=True)
                ws.send(prefix + segmented)
                # print('successfully sent', flush=True)
                translated = ws.recv().replace('@@ ','')
                # print('translated sentence ' + translated, flush=True)
                detokenized = detokenizer[toLang](translated.split())
                print('TRANSLATION: ' + detokenized, flush=True)
            message.append(detokenized)
            cache[key] = detokenized
        trgtext = ' '.join(message)
        data = {'result': trgtext, 'source': fromLang, 'target': toLang}
        # 'origin': "ws://{}:{}/translate".format(args.mthost, args.mtport)}
        self.sendMessage(json.dumps(data, sort_keys=True, indent=4))
        # self.request.sendall(bytes(json.dumps(data, sort_keys=True, indent=4), "utf-8"))
        # self.request.sendall(bytes(trgtext, "utf-8"))
        cache[srctxt] = trgtext

    def handleConnected(self):
        print(self.address, 'connected')

    def handleClose(self):
        print(self.address, 'closed')





print("listen on socket " + str(args.port))
server = SimpleWebSocketServer('', args.port, Translate)
server.serveforever()


# if __name__ == "__main__":

    ## using TCP Socket Server instead
    ## Create the server, binding to localhost on port 9999
    # server = socketserver.TCPServer(("localhost", args.port), Translate)

    ## Activate the server; this will keep running until you
    ## interrupt the program with Ctrl-C
    # server.serve_forever()