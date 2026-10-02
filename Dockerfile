FROM freqtradeorg/freqtrade:stable
COPY --chown=ftuser:ftuser user_data/config.json /freqtrade/user_data/config.json
COPY --chown=ftuser:ftuser user_data/strategies/ /freqtrade/user_data/strategies/
COPY --chown=ftuser:ftuser grid/ /freqtrade/grid/
COPY --chown=ftuser:ftuser start.sh /freqtrade/start.sh
ENTRYPOINT ["/bin/bash", "/freqtrade/start.sh"]
