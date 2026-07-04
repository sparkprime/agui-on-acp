#!/bin/bash

DIRS="./tests ./agui_on_acp"

isort $DIRS
black $DIRS
