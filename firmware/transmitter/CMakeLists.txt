# Minimum CMake version required by ESP-IDF
cmake_minimum_required(VERSION 3.16)

# Always add colour to compiler error messages
add_compile_options(-fdiagnostics-color=always)

# Pull in ESP-IDF build system
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# Project name – taken automatically from the directory name
project(csi_transmitter)
