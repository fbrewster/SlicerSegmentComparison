import logging
import pathlib
import shutil
from typing import Annotated, Optional
import pathlib
import typing
import vtk
from scipy.spatial import KDTree
import numpy as np

import slicer # pylint: disable=import-error
from slicer.i18n import tr as _ # pylint: disable=import-error
from slicer.i18n import translate # pylint: disable=import-error
from slicer.ScriptedLoadableModule import * # pylint: disable=import-error
from slicer.util import VTKObservationMixin # pylint: disable=import-error
from slicer.parameterNodeWrapper import parameterNodeWrapper, Choice, Minimum # pylint: disable=import-error
from slicer import vtkMRMLSegmentationNode # pylint: disable=import-error

try:
    import pandas as pd
except:
    slicer.util.pip_install('pandas')
    import pandas as pd

#
# BLD
#


class BLD(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Bidirectional Local Distance")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Quantification")]
        self.parent.dependencies = []
        self.parent.contributors = [
            "Frank Brewster (The Christie NHS Foundation Trust)",
            "Alberto Salas Mellado (Hospital Clínico Universitario de Santiago)"
            ]
        self.parent.helpText = _("""
This module implements a <a href="https://doi.org/10.1118/1.4754802">bidirectional local distance (BLD)</a> measure between two segments.
A BLD vector is calculated for each point on segment A.
The vertices on A are then split into regions correseponding to the most distal vertices in each of the cardinal directions. This is done by
calculating the extents of the mesh and then taking vertices which have a component coordinate within a margin of this extent.
The distance for a direction is taken as the maximum vector component in that direction. The mode selector allows for an absolute maximum (compare),
S/A/R max (+ve) and I/P/L min (-ve) (grow) or S/A/R min (-ve) and I/P/L max (+ve) (shrink).
The maximum magnitude vector is taken as the Haussdorff distance and the 95th percentiule magnitude as the HD95.
The draw option will show the vectors used in these max calcalutions.

The function traverses contour points rather than the surface. As a result, there may be some inaccurcies for contours that have lots of interpolation/few points.
The resample function can reduce this effect by converting to a binary label map and resampling the points with smoothing but will overwrite the original contour.                           
""")
        self.parent.acknowledgementText = _("""
Frank Brewster was supported by the NIHR Manchester Biomedical Research Centre (NIHR203308).
The template file used for this module was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc., Andras Lasso, PerkLab, and Steve Pieper, Isomics, Inc. and was partially funded by NIH grant 3P41RR013218-12S1.
""")


#
# BLDParameterNode
#


@parameterNodeWrapper
class BLDParameterNode:
    """
    The parameters needed by module (supported by the parameter node wrapper).

    regionMargin - the margin used when allocating vertices to a side of a segment
    drawVectors - whether to draw the vectors used to calculate the distances
    resample - whether to convert the segments to binary labelmap and resample the points
    mode - How to calculate the maximum distance ['Compare', 'Grow', 'Shrink']
    """
    regionMargin: Annotated[float, Minimum(0.1)] = 20
    drawVectors: bool = True
    resample: bool = False
    mode: Annotated[str, Choice(['Compare','Grow','Shrink'])] = 'Compare'


#
# BLDWidget
#


class BLDWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
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
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/BLD.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = BLDLogic()

        # Connections

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Buttons
        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)
        self.ui.exportButton.connect("clicked(bool)", self.onExportButton)

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
        # Do not react to parameter node changes
        # (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None
            self.removeObserver(self._parameterNode,
                                vtk.vtkCommand.ModifiedEvent,
                                self._checkCanApply)

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)
        if self.logic.results:
            for direction in self.logic.results.keys():
                label = eval(f"self.ui.{direction}Label")
                label.text = '-'
            self.ui.HDLabel.text = '-'
            self.ui.HD95Label.text = '-'

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node
        # immediately
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        # parameter nodes do not yet support segmentation selectors
        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[BLDParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then 
        the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self.removeObserver(self._parameterNode,
                                vtk.vtkCommand.ModifiedEvent,
                                self._checkCanApply)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on
            # each ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)
            self._checkCanApply()

    def _checkCanApply(self, caller=None, event=None) -> None:
        if self._parameterNode and \
            self.ui.segmentSelectorA.currentNode() and \
                self.ui.segmentSelectorB.currentNode():
            self.ui.applyButton.toolTip = _("Compute output")
            self.ui.applyButton.enabled = True
        else:
            self.ui.applyButton.toolTip = _("Select input nodes")
            self.ui.applyButton.enabled = False

    def _updateProg(self, value):
        self.ui.applyButton.text = value
        slicer.app.processEvents()

    def onApplyButton(self) -> None:
        """Run processing when user clicks "Apply" button."""
        with slicer.util.tryWithErrorDisplay(_("Failed to compute results."), waitCursor=True):
            print(self._parameterNode)
            self.logic.updateProg = self._updateProg
            # Compute output
            self.logic.process(self.ui.segmentSelectorA.currentNode(),
                               self.ui.segmentSelectorA.currentSegmentID(),
                               self.ui.segmentSelectorB.currentNode(),
                               self.ui.segmentSelectorB.currentSegmentID())

            if self.logic.results:
                for direction, dist in self.logic.results.items():
                    label = eval(f"self.ui.{direction}Label")
                    label.text = f"{dist:.1f} mm"
                self.ui.HDLabel.text = f"{self.logic.hd:.1f} mm"
                self.ui.HD95Label.text = f"{self.logic.hd95:.1f} mm"

            if self._parameterNode.drawVectors and self.logic.vectorResults:
                for d,arr in self.logic.vectorResults.items():
                    line = slicer.util.getFirstNodeByName(d,className='vtkMRMLMarkupsLineNode')
                    if not line:
                        line = slicer.mrmlScene.AddNewNodeByClass('vtkMRMLMarkupsLineNode')
                        line.SetName(d)
                    slicer.util.updateMarkupsControlPointsFromArray(line, arr)
        self.ui.exportButton.enabled = True
        self._updateProg("Apply")

    def onExportButton(self) -> None:
        with slicer.util.tryWithErrorDisplay(_("Failed to export results.")):
            if self.ui.appendRadio.checked:
                self.logic.pushToFile(self.ui.filePicker.currentPath)
            else:
                self.logic.pushToFile(self.ui.pathPicker.directory,
                                      self.ui.fileFormatBox.currentText)


#
# BLDLogic
#


class BLDLogic(ScriptedLoadableModuleLogic):
    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)
        self.newFileName = "BLD_Results"
        self.results = {}
        self.vectorResults = {}
        self.segAName = ''
        self.segBName = ''
        self.regionMargin = 5
        self.nOfVerts = None
        self.hd = None
        self.hd95 = None

        self._absMax = lambda x: max(np.max(x), np.min(x), key=abs)

    def getParameterNode(self):
        return BLDParameterNode(super().getParameterNode())

    def calcBiDiDist(self, df1Row, df2):
        """
        Calculate the bidirectional local distance for a vertex.
        Arbitrary parameters make this semi-private but can be used public if desired.
        :param df1Row: a Pandas Series of information for a vertex on mesh 1    
            Dist: float - the NN distance calculated for this vertex
            TargCoord - usually the coordinates of the target NN vertex but the actual coordinates are not used so this could be symbollic.
        :param df2: a Pandas DataFrame of NN distances and coordinates for mesh 2
            Coord - the coordinate of the vertex on mesh 2 but the actual coordinates are not used so this could be symbollic.
            Dist: float - the NN distance calculated for this vertex
            TargIndex - index of the target point (on mesh 1)
        
        :returns: Dict containing the bidirectional local distance and target coordinate.
        """
        self.updateProg(f"Looking for bidirectional maxima for vertex {df1Row['OrdinalIndex']}/{self.nOfVerts}")

        pointsOn2ForThisVert = df2[df2['TargIndex']==df1Row.name]

        if pointsOn2ForThisVert.shape[0]>0 and max(pointsOn2ForThisVert['Dist'])>df1Row['Dist']:
            indexOfMax = pointsOn2ForThisVert.idxmax()['Dist']
            targetRowOn2 = pointsOn2ForThisVert.loc[indexOfMax]
            return {'BiDiDist': targetRowOn2['Dist'], 'BiDiTargetCoord': targetRowOn2['Coord']}

        return {'BiDiDist': df1Row['Dist'], 'BiDiTargetCoord': df1Row['TargCoord']}

    def _allocateRegion(self, vert, extents):
        """
        Allocate vertices to a region.
        Vertices are assigned to a region if they are between the extent and the extent-self.regionMargin
        :param vert: the coordinates of a vertex
        :param extent: the maximum extents of the mesh as a dict of cardinal direcitons
        """
        if vert[0]>extents['R']-self.regionMargin:
            return 'R'
        elif vert[0]<extents['L']+self.regionMargin:
            return 'L'
        elif vert[1]>extents['A']-self.regionMargin:
            return 'A'
        elif vert[1]<extents['P']+self.regionMargin:
            return 'P'
        elif vert[2]>extents['S']-self.regionMargin:
            return 'S'
        elif vert[2]<extents['I']+self.regionMargin:
            return 'I'
        return 'N'

    def process(self,
                segANode: vtkMRMLSegmentationNode,
                segAID: str,
                segBNode: vtkMRMLSegmentationNode,
                segBID: str
                ) -> None:
        """
        Run the processing algorithm.
        Can be used without GUI widget.
        :param segAnode: segmentation node containing segment A
        :param segAID: segment A's ID
        :param segBnode: segmentation node containing segment B
        :param segBID: segment B's ID
        """

        if not segANode or not segBNode:
            raise ValueError("Input or output volume is invalid")

        if segANode==segBNode and segAID==segBID:
            raise ValueError("Cannot compare structre to itself")

        import time

        startTime = time.time()
        logging.info("Processing started")
        self.updateProg("Building lookup trees")

        parameterNode = self.getParameterNode()
        self.regionMargin = parameterNode.regionMargin
        resample = parameterNode.resample
        mode = parameterNode.mode

        # Force oversampling for smoothness
        if resample:
            segANode.CreateBinaryLabelmapRepresentation()
            segANode.SetSourceRepresentationToBinaryLabelmap()
            segANode.RemoveClosedSurfaceRepresentation()
            if segANode != segBNode:
                segBNode.CreateBinaryLabelmapRepresentation()
                segBNode.SetSourceRepresentationToBinaryLabelmap()
                segBNode.RemoveClosedSurfaceRepresentation()

        # Get vertices
        segANode.CreateClosedSurfaceRepresentation()
        cs1 = vtk.vtkPolyData()
        segANode.GetClosedSurfaceRepresentation(segAID, cs1)
        verts1 = vtk.util.numpy_support.vtk_to_numpy(cs1.GetPoints().GetData())

        segBNode.CreateClosedSurfaceRepresentation()
        cs2 = vtk.vtkPolyData()
        segBNode.GetClosedSurfaceRepresentation(segBID, cs2)
        verts2 = vtk.util.numpy_support.vtk_to_numpy(cs2.GetPoints().GetData())

        # Make lookup trees
        lu1 = KDTree(verts1)
        lu2 = KDTree(verts2)

        # Query trees
        dist12, targIndexOn2 = lu2.query(verts1)  # look at 1, return NN dist to 2 + index of target
        targetsOn2From1 = lu2.data[targIndexOn2]
        pointsOn1DF = pd.DataFrame({
            'Coord': verts1.tolist(),
            'Dist': dist12,
            'TargIndex': targIndexOn2,
            'TargCoord': targetsOn2From1.tolist()})

        dist21, targIndexOn1 = lu1.query(verts2)
        targetsOn1From2 = lu1.data[targIndexOn1]
        pointsOn2DF = pd.DataFrame({
            'Coord': verts2.tolist(),
            'Dist': dist21,
            'TargIndex': targIndexOn1,
            'TargCoord': targetsOn1From2.tolist()
            })

        # Assign regions
        self.updateProg("Allocating directional regions")
        extents = {
                'R':np.max(verts1[:,0]),
                'L':np.min(verts1[:,0]),
                'A':np.max(verts1[:,1]),
                'P':np.min(verts1[:,1]),
                'S':np.max(verts1[:,2]),
                'I':np.min(verts1[:,2])}
        pointsOn1DF['Region'] = pointsOn1DF.apply(
            lambda x,e: self._allocateRegion(x['Coord'],e),
            args=[extents],
            axis=1)

        # Drop the verts outside a directional region
        pointsOn1DF = pointsOn1DF[pointsOn1DF['Region'] != 'N']
        self.nOfVerts = pointsOn1DF.shape[0]
        pointsOn1DF['OrdinalIndex'] = np.arange(1, self.nOfVerts + 1)  # only used to set progress

        # Check for bidirectional maximas from 2 to 1
        pointsOn1DF[['BiDiDist','BiDiTargetCoord']] = pointsOn1DF.apply(
            self.calcBiDiDist,
            args=[pointsOn2DF],
            axis=1,
            result_type='expand')

        self.updateProg("Extracting vectors")

        # Calculate the vector between bidirectional NNs
        pointsOn1DF['Vector'] = pointsOn1DF.apply(
            lambda x:np.array(x['BiDiTargetCoord'])-np.array(x['Coord']),axis=1)
        vector_array = np.vstack(pointsOn1DF['Vector'])
        vector_df = pd.DataFrame(vector_array, index=pointsOn1DF.index, columns=['RL', 'AP', 'SI'])
        pointsOn1DF = pointsOn1DF.join(vector_df)
        # pointsOn1DF.to_csv('debug_data.csv')

        # How to take the 'maximum'
        if mode=='c' or mode=='Compare':  # max ignore direction
            posMaxFunc = self._absMax
            negMaxFunc = self._absMax
        elif mode=='g' or mode=='Grow':  # only expansions from a to b
            posMaxFunc = max
            negMaxFunc = min
        elif mode=='s' or mode=='Shrink':  # only shrinks from a to b
            posMaxFunc = min
            negMaxFunc = max
        else:
            raise ValueError(f'Invalid mode, {mode}, see documentation')


        self.hd = np.max(pointsOn1DF['BiDiDist'])
        self.hd95 = np.percentile(pointsOn1DF['BiDiDist'],95)

        self.segAName = segANode.GetSegmentation().GetSegment(segAID).GetName()
        self.segBName = segBNode.GetSegmentation().GetSegment(segBID).GetName()

        # Find the vector for each of the maxes
        for d in [['R','L'],['A','P'],['S','I']]:
            dCombined = "".join(d)

            # +ve region
            region0 = pointsOn1DF[pointsOn1DF['Region']==d[0]]
            dist0 = posMaxFunc(region0[dCombined])
            index0 = region0[region0[dCombined]==dist0].index[0]
            self.vectorResults[d[0]] = np.array([
                region0.loc[index0]['BiDiTargetCoord'],
                region0.loc[index0]['Coord']
                ])
            self.results[d[0]] = dist0

            # -ve region
            region1 = pointsOn1DF[pointsOn1DF['Region']==d[1]]
            dist1 = negMaxFunc(region1[dCombined])
            index1 = region1[region1[dCombined]==dist1].index[0]
            self.vectorResults[d[1]] = np.array([
                region1.loc[index1]['BiDiTargetCoord'],
                region1.loc[index1]['Coord']
                ])
            self.results[d[1]] = dist1*-1

        stopTime = time.time()
        logging.info(f"Processing completed in {stopTime-startTime:.2f} seconds")

    def pushToFile(self, 
                   f_name: typing.Union[str, pathlib.Path],
                   format: str = None) -> None:
        """
        Push the results stored in the object to file.
        :param f_name: the file name to write/append to
        :param format: a string representing the format of the file to write ['CSV','Pickle','Excel'], if None then append.
        """
        if not self.results:
            ValueError("Cannot export as no results have been calculated")

        if type(f_name) is str:
            path = pathlib.Path(f_name)
        else:
            path = f_name

        # Make a dict storing this set of results, including segment names and HDs
        results = {}
        results['SegmentA'] = self.segAName
        results['SegmentB'] = self.segBName
        results['HD'] = self.hd
        results['HD95'] = self.hd95
        results = results | self.results
        new_df = pd.DataFrame(results, index=[0])

        if format:  # New file
            if format=='CSV':
                new_df.to_csv(path / f"{self.newFileName}.csv", index=False)
            elif format=='Pickle':
                new_df.to_pickle(path / f"{self.newFileName}.pkl")
            elif format=='Excel':
                new_df.to_excel(path / f"{self.newFileName}.xlsx", index=False)
            else:
                raise ValueError("Unrecognised file format")
        else:  # Append
            ex = path.suffix
            if ex=='.csv':
                df = pd.read_csv(path)
                df = pd.concat([df, new_df], ignore_index=True)
                df.to_csv(path, index=False)
            elif ex=='.pkl':
                df = pd.read_pickle(path)
                df = pd.concat([df, new_df])
                df.to_pickle(path, index=False)
            elif ex=='.xlsx':
                df = pd.read_excel(path)
                df = pd.concat([df, new_df], ignore_index=True)
                df.to_excel(path, index=False)
            else:
                raise ValueError("Unrecognised file format")


#
# BLDTest
#


class BLDTest(ScriptedLoadableModuleTest):
    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
        self.test_LoadData()
        self.test_resampleContours()
        self.test_convertContours()
        self.test_fullProcess()
        self.cleanUp()
        self.delayDisplay("All tests passed")

    def test_LoadData(self):
        """Load the test data"""
        import SampleData
        [self.volumeNode, self.segNode] = SampleData.downloadSamples("TinyPatient")

        seg = self.segNode.GetSegmentation()
        self.segAID = seg.GetNthSegmentID(0)
        self.segBID = seg.GetNthSegmentID(1)

        self.delayDisplay("Loaded test data set")


    def test_resampleContours(self):
        """Test the conversion to binary labelmap and back to closed surface"""
        self.delayDisplay("Testing resampling")
        self.segNode.CreateBinaryLabelmapRepresentation()
        self.segNode.SetSourceRepresentationToBinaryLabelmap()
        self.segNode.RemoveClosedSurfaceRepresentation()
        self.segNode.CreateClosedSurfaceRepresentation()
        self.delayDisplay("Test passed")

    def test_convertContours(self):
        """Test extracting vtkPolyData"""
        self.delayDisplay("Testing contour point extraction")
        cs1 = vtk.vtkPolyData()
        self.segNode.GetClosedSurfaceRepresentation(self.segAID, cs1)
        vertsA = vtk.util.numpy_support.vtk_to_numpy(cs1.GetPoints().GetData())

        self.segNode.CreateClosedSurfaceRepresentation()
        cs2 = vtk.vtkPolyData()
        self.segNode.GetClosedSurfaceRepresentation(self.segBID, cs2)
        vertsB = vtk.util.numpy_support.vtk_to_numpy(cs2.GetPoints().GetData())
        self.delayDisplay("Test passed")

    def test_fullProcess(self):
        """Test the full processing from logic"""
        self.delayDisplay("Testing full analysis")
        logic = BLDLogic()
        logic.updateProg = lambda s: None
        logic.process(self.segNode, self.segAID, self.segNode, self.segBID)
        self.delayDisplay("Test passed")

    def test_exports(self):
        """Test export of results to file"""
        self.delayDisplay("Testing file output")

        path = pathlib.Path("/tmp")
        path.mkdir()
        # New Files
        self.logic.pushToFile("TestFile", 'CSV')
        self.logic.pushToFile("TestFile", 'Excel')
        self.logic.pushToFile("TestFile", 'Pickel')

        csvPath = path / "/tmp/TestFile.csv"
        xlPath = path / "/tmp/TestFile.xlsx"
        pklPath = path / "/tmp/TestFile.pkl"

        # Appends
        self.logic.pushToFile(csvPath)
        self.logic.pushToFile(xlPath)
        self.logic.pushToFile(pklPath)
        self.delayDisplay("Test passed")

        csvPath.unlink()
        xlPath.unlink()
        pklPath.unlink()
        path.rmdir()

    def cleanUp(self):
        slicer.mrmlScene.RemoveNode(self.segNode)
        slicer.mrmlScene.RemoveNode(self.volumeNode)
