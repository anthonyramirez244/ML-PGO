#!/bin/bash

SOURCE_DIR=$(pwd)
DIR=""
SPACK_DIR=""

if [ $# -eq 0 ]; then
  DIR=$(pwd)/gpa
else
  if [ $# -eq 1 ]; then
    DIR=$1
  else
    if [ $# -eq 2 ]; then
      DIR=$1
      SPACK_DIR=$2
    fi
  fi
fi

if [ -z "$DIR" ]; then
  echo $DIR
  echo $SPACK_DIR
  echo "Wrong prefix"
  exit
fi

mkdir $DIR
cd $DIR

# Install spack
if [ -z $SPACK_DIR ]; then
  git clone https://github.com/spack/spack.git
  export SPACK_ROOT=$(pwd)/spack
  export PATH=${SPACK_ROOT}/bin:${PATH}
  source ${SPACK_ROOT}/share/spack/setup-env.sh

  # Install hpctoolkit dependencies
  spack install --only dependencies hpctoolkit ^dyninst@master ^binutils@2.41+libiberty~nls ^papi~rocp_sdk ^ucx~backtrace_detail~rocm~cuda
  spack install libmonitor@master+dlopen+hpctoolkit
  spack install mbedtls gotcha

  # HPCToolkit needs a real compiled libboost_system; boost >= 1.85 made it
  # header-only and dropped the library. Build a separate boost just for this
  # (standalone, so it doesn't force a rebuild of dyninst's already-cached boost).
  # visibility=global: HPCToolkit's GraphReader.cpp calls boost's internal
  # read_graphviz_detail::parse_graphviz_from_string() directly, which isn't
  # marked for export under boost's default hidden-visibility build.
  spack install boost@1.84.0+system+filesystem+graph+regex+thread+timer+atomic+chrono+date_time~mpi~python~numpy visibility=global

  # Find spack dir
  B=$(spack find --path boost | tail -n 1 | cut -d ' ' -f 3)
  SPACK_DIR=${B%/*}
  BOOST_PATH=$(spack location -i boost@1.84.0)
fi

CUDA_PATH=/usr/local/cuda/
CUPTI_PATH=$CUDA_PATH/extras/CUPTI/

# install hpctoolkit
cd $SOURCE_DIR
cd hpctoolkit
mkdir build
cd build
../configure --prefix=$DIR/hpctoolkit --with-cuda=$CUDA_PATH \
  --with-cupti=$CUPTI_PATH --with-spack=$SPACK_DIR --with-boost=$BOOST_PATH
make install -j8

echo "Install in "$DIR"/hpctoolkit"

cd $SOURCE_DIR
cp -rf ./bin $DIR
export PATH=$DIR/bin:${PATH}
