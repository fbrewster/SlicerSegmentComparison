import logging
import os
from typing import Annotated, Optional

import vtk
import qt

#from PyQt6.QtWidgets import QTableWidgetItem

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import (
    parameterNodeWrapper,
    WithinRange,
)

from slicer import vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode

import SimpleITK as sitk
import sitkUtils

def _get_lps_to_ras_matrix():
    lpsToRas = vtk.vtkMatrix4x4()
    lpsToRas.Identity()
    lpsToRas.SetElement(0, 0, -1)
    lpsToRas.SetElement(1, 1, -1)
    return lpsToRas

#
# STAPLE
#


class STAPLE(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("STAPLE")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Quantification")]
        self.parent.dependencies = []
        self.parent.contributors = ["Frank Brewster (The Christie NHS Foundation Trust)"] 
        self.parent.helpText = _("""
This module exposes SITK's implementation of the Simultaneous Truth and Performance Level Estimation 
algorithm for generating ground truth volumes from a set of binary expert segmentations as decribed in
                                 
S. Warfield, K. Zou, W. Wells, "Validation of image segmentation and expert quality with an expectation-maximization algorithm" 
in MICCAI 2002: Fifth International Conference on Medical Image Computing and Computer-Assisted Intervention, 
Springer-Verlag, Heidelberg, Germany, 2002, pp. 298-306
""")
        # TODO: replace with organization, grant and thanks
        self.parent.acknowledgementText = _("""
Frank Brewster was supported by the NIHR Manchester Biomedical Research Centre (NIHR203308).
The template file used for this module was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc., Andras Lasso, PerkLab,
and Steve Pieper, Isomics, Inc. and was partially funded by NIH grant 3P41RR013218-12S1.
""")


#
# STAPLEParameterNode
#


@parameterNodeWrapper
class STAPLEParameterNode:
    """
    The parameters needed by module.

    inputSeg - The segmentations to be included
    imageThreshold - The value at which to threshold the STAPLE probabilities
    """
    # TODO: write docstring

    inputSeg: vtkMRMLSegmentationNode
    outputDest: vtkMRMLSegmentationNode
    imageThreshold: Annotated[float, WithinRange(0, 1)] = 0.5
    includeVisable: bool = True
    refImg: vtkMRMLScalarVolumeNode


#
# STAPLEWidget
#


class STAPLEWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/STAPLE.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = STAPLELogic()

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

        # Select default input nodes if nothing is selected yet to save a few clicks for the user
        if not self._parameterNode.inputSeg:
            firstSegNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
            if firstSegNode:
                self._parameterNode.inputSeg = firstSegNode

    def setParameterNode(self, inputParameterNode: Optional[STAPLEParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._checkCanApply()

    def _checkCanApply(self, caller=None, event=None) -> None:
        if self._parameterNode and self._parameterNode.inputSeg:
            self.ui.applyButton.toolTip = _("Compute output volume")
            self.ui.applyButton.enabled = True
        else:
            self.ui.applyButton.toolTip = _("Select input and output volume nodes")
            self.ui.applyButton.enabled = False

    def _updateProg(self, value):
        self.ui.applyButton.text = value
        slicer.app.processEvents()

    def onApplyButton(self) -> None:
        """Run processing when user clicks "Apply" button."""
        self.ui.applyButton.enabled = False
        with slicer.util.tryWithErrorDisplay(_("Failed to compute results."), waitCursor=True):
            self.logic.updateProg = self._updateProg
            # Compute output
            self.logic.process(self.ui.inputSelector.currentNode(),
                               self.ui.outputSelector.currentNode(),
                               self.ui.SegmentsTableView.selectedSegmentIDs())

            # Make table
            results = self.logic.results
            self.ui.resultsTable.sortingEnabled = False  # stops table from reorganising while data is entered
            self.ui.resultsTable.setRowCount(len(results["Names"]))
            colNum = 0
            for col in results.values():
                rowNum = 0
                for item in col:
                    if isinstance(item, float): item = f"{round(item*100, 3)}%"
                    self.ui.resultsTable.setItem(rowNum, colNum, qt.QTableWidgetItem(item))
                    rowNum += 1
                colNum += 1
            self.ui.resultsTable.sortingEnabled = True
        self._updateProg("Apply")
        self.ui.applyButton.enabled = True

#
# STAPLELogic
#


class STAPLELogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)
        self.results = {}

    def getParameterNode(self):
        return STAPLEParameterNode(super().getParameterNode())
    
    def _removeNodeWithDisplay(self,node):
        displayNode = node.GetDisplayNode()
        if displayNode:
            slicer.mrmlScene.RemoveNode(displayNode)
        slicer.mrmlScene.RemoveNode(node)

    def process(self,
                inputSeg: vtkMRMLSegmentationNode,
                outputDest: vtkMRMLSegmentationNode,
                segmentIDs: list[str]) -> None:
        """
        Run the processing algorithm.
        Can be used without GUI widget.
        :param inputSeg: segmentations to be used
        :param outputDest: the segmentation node to add the STAPLE to (or None if new)
        :param segmentIDs: a list of segment IDs to inlcude or None to include all visibles
        """

        if not inputSeg:
            raise ValueError("Input segmentation is invalid")

        import time

        startTime = time.time()
        logging.info("Processing started")

        imageThreshold = self.getParameterNode().imageThreshold
        includeVisable = self.getParameterNode().includeVisable

        segLogic = slicer.modules.segmentations.logic()

        # Get reference image
        refImg = inputSeg.GetNodeReference(inputSeg.GetReferenceImageGeometryReferenceRole())

        # Get binary respresentations
        inputSeg.CreateBinaryLabelmapRepresentation()
        inputSeg.SetSourceRepresentationToBinaryLabelmap()

        # Get list of segment IDs
        segmentation = inputSeg.GetSegmentation()
        if includeVisable:
            segmentIDs = segmentation.GetSegmentIDs()

        if len(segmentIDs)<2:
            raise ValueError("Only 1 segment selected/visable")


        # Output each segment to a labelmap and convert to sitk
        # (sitk can't pull the binary label map representation directly)
        # TODO: convert binary labelmaps from vtkOrientedData to sitk directly. Through array? Don't need spatial info if they're all in the same spacing anyway?
        labelmaps = {}
        thisLabelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode")
        displayNode = inputSeg.GetDisplayNode()
        foundSegment = False
        i = 1
        nOfSegs = len(segmentIDs)
        for thisSegmentID in segmentIDs:
            self.updateProg(f"Converting segment {i}/{nOfSegs} to SITK")
            if includeVisable and not displayNode.GetSegmentVisibility(thisSegmentID):
                continue  # skip if not visable

            thisSegmentName = segmentation.GetSegment(thisSegmentID).GetName()

            dummmyIDList = [thisSegmentID]  # overlapping so can't pull all at once
            segLogic.ExportSegmentsToLabelmapNode(inputSeg, dummmyIDList, thisLabelmapNode, refImg)
            thisLabelmap = sitkUtils.PullVolumeFromSlicer(thisLabelmapNode)
            labelmaps[thisSegmentName] = thisLabelmap
            foundSegment = True
            i += 1

        if not foundSegment:
            raise ValueError("No segments found to be included")

        self._removeNodeWithDisplay(thisLabelmapNode)

        # Run STAPLE filter
        self.updateProg("Running STAPLE filter")
        stapleFilter = sitk.STAPLEImageFilter()
        stapleProbs = stapleFilter.Execute(list(labelmaps.values()))
        stapleMask = sitk.BinaryThreshold(stapleProbs, imageThreshold, 1, 1, 0)

        # Output to label map node
        self.updateProg("Converting STAPLE mask back to labelmap")
        stapleLabelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode")
        stapleLabelmapNode.SetName("STAPLE Lablemap")
        stapleLabelmapNode.CreateDefaultDisplayNodes()
        stapleLabelmapNode = sitkUtils.PushVolumeToSlicer(stapleMask, stapleLabelmapNode)

        # Set up segmentation node if needed
        self.updateProg("Converting labelmap to segmentation")
        if outputDest is None:
            outputDest = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
            outputDest.AddToSceneOn()

        # Setup segmentaion colour table for label map import to give segmentation the right name
        # Colour is irrelavent, just a way to map labelmap values to a name
        colourTableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode")
        colourTableNode.SetTypeToUser()
        colourTableNode.SetNumberOfColors(2)
        colourTableNode.SetColor(0, 0, 0, 0, 0)  # background (0) is black
        colourTableNode.SetColor(1, 1, 0, 0, 1)  # staple (1) is red
        colourTableNode.SetColorName(0, "Background")
        colourTableNode.SetColorName(1,"STAPLE")
        stapleLabelmapNode.GetDisplayNode().SetAndObserveColorNodeID(colourTableNode.GetID())

        slicer.vtkSlicerSegmentationsModuleLogic.ImportLabelmapToSegmentationNode(
            stapleLabelmapNode, outputDest)

        # Clean up nodes
        # TODO: remove colour table
        self._removeNodeWithDisplay(stapleLabelmapNode)
        slicer.mrmlScene.RemoveNode(colourTableNode)

        # Get values for table
        results = {}
        results["Names"] = list(labelmaps.keys())
        results["Sensitivity"] = stapleFilter.GetSensitivity()
        results["Specificity"] = stapleFilter.GetSpecificity()
        self.results = results

        stopTime = time.time()
        logging.info(f"Processing completed in {stopTime-startTime:.2f} seconds")


#
# STAPLETest
#

# TODO: implement tests
class STAPLETest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
        self.test_LoadData()
        self.test_ConvertToSITK()
        self.test_RunSTAPLE()
        self.test_LabelMapOutput()
        self.test_LabelMapToSeg()
        self.cleanUp()
        self.delayDisplay("All tests passed")

    def test_LoadData(self):
        """Load the test data"""
        import SampleData
        [self.volumeNode, self.segNode] = SampleData.downloadSamples("TinyPatient")

        self.seg = self.segNode.GetSegmentation()
        self.segAID = self.seg.GetNthSegmentID(0)
        self.segBID = self.seg.GetNthSegmentID(1)

        self.delayDisplay("Loaded test data set")

    def test_ConvertToSITK(self):
        self.delayDisplay("Testing SITK conversion")
        segmentIDs = self.seg.GetSegmentIDs()

        segLogic = slicer.modules.segmentations.logic()

        self.labelmaps = {}
        thisLabelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode")
        displayNode = self.segNode.GetDisplayNode()
        foundSegment = False

        for thisSegmentID in segmentIDs:
            thisSegmentName = self.seg.GetSegment(thisSegmentID).GetName()

            dummmyIDList = [thisSegmentID]  # overlapping so can't pull all at once
            segLogic.ExportSegmentsToLabelmapNode(self.segNode, dummmyIDList, thisLabelmapNode, self.volumeNode)
            thisLabelmap = sitkUtils.PullVolumeFromSlicer(thisLabelmapNode)
            self.labelmaps[thisSegmentName] = thisLabelmap

        self.delayDisplay("Test passed")

    def test_RunSTAPLE(self):
        self.delayDisplay("Testing STAPLE filter")
        stapleFilter = sitk.STAPLEImageFilter()
        stapleProbs = stapleFilter.Execute(list(self.labelmaps.values()))
        self.stapleMask = sitk.BinaryThreshold(stapleProbs, 0.5, 1, 1, 0)
        self.delayDisplay("Test passed")

    def test_LabelMapOutput(self):
        self.delayDisplay("Testing labelmap output")
        self.stapleLabelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode")
        self.stapleLabelmapNode.SetName("STAPLE Lablemap")
        self.stapleLabelmapNode.CreateDefaultDisplayNodes()
        self.stapleLabelmapNode = sitkUtils.PushVolumeToSlicer(self.stapleMask, self.stapleLabelmapNode)
        self.delayDisplay("Test passed")

    def test_LabelMapToSeg(self):
        self.delayDisplay("Testing segmentation conversion")
        outputDest = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        outputDest.AddToSceneOn()

        colourTableNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode")
        colourTableNode.SetTypeToUser()
        colourTableNode.SetNumberOfColors(2)
        colourTableNode.SetColor(0, 0, 0, 0, 0)  # background (0) is black
        colourTableNode.SetColor(1, 1, 0, 0, 1)  # staple (1) is red
        colourTableNode.SetColorName(0, "Background")
        colourTableNode.SetColorName(1,"STAPLE")
        self.stapleLabelmapNode.GetDisplayNode().SetAndObserveColorNodeID(colourTableNode.GetID())

        slicer.vtkSlicerSegmentationsModuleLogic.ImportLabelmapToSegmentationNode(
            self.stapleLabelmapNode, outputDest)
        self.delayDisplay("Test passed")
        
    def cleanUp(self):
        slicer.mrmlScene.Clear()
