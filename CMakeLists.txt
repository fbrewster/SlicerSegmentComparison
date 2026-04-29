cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SegmentComparison)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/fbrewster/SlicerSegmentComparison")
set(EXTENSION_CONTRIBUTORS "Frank Brewster (The Christie NHS Foundation Trust)")
set(EXTENSION_DESCRIPTION "Tools for comparing segmentations")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/fbrewster/SlicerSegmentComparison/main/SegmentComparison.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/fbrewster/SlicerSegmentComparison/main/BLDGeneralScreenshot.png https://raw.githubusercontent.com/fbrewster/SlicerSegmentComparison/main/STAPLEGeneralScreenshot.png")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(STAPLE)
add_subdirectory(BLD)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
