#!/bin/sh
# Baseline every capture with the current Python p25-survey / op25 decoder.
cd ~/p25-corpus
CC=851.0875,851.3625,851.9625,852.2375,852.65,853.7375,853.7875,853.9125,854.7875,855.0625,855.2375,855.4125,856.4375,857.2875,857.8375,858.1375,858.4375,858.8125,859.2125,859.7125,859.7375,774.48125
for f in raw/*.sigmf-data; do
  b=${f%.sigmf-data}
  python3 baseline.py "$b" --cc $CC 2>>baseline/op25-stderr.log
done
echo ALL DONE
