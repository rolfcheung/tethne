import logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel('DEBUG')

import numpy as np
import numpy
import tables
import matplotlib.pyplot as plt
from paper import Paper
from collections import Counter
from nltk.corpus import stopwords
import scipy as sc
import tempfile
import uuid

from ..utilities import strip_punctuation
from ..classes import HDF5Paper

class DataCollection(object):
    """
    A :class:`.DataCollection` organizes :class:`.Paper`\s for analysis.
    
    The :class:`.DataCollection` is initialized with some data, which is indexed
    by a key in :class:`.Paper` (default is wosid). The :class:`.DataCollection`
    can then be sliced ( :func:`DataCollection.slice` ) by other keys in
    :class:`.Paper` .
    
    **Usage**
    
    .. code-block:: python

       >>> import tethne.readers as rd
       >>> data = rd.wos.read("/Path/to/wos/data.txt")
       >>> data += rd.wos.read("/Path/to/wos/data2.txt")    # Two accessions.
       >>> from tethne.data import DataCollection
       >>> D = DataCollection(data) # Indexed by wosid, by default.
       >>> D.slice('date', 'time_window', window_size=4)
       >>> D.slice('accession')
       >>> D
       <tethne.data.DataCollection at 0x10af0ef50>
       
       
    Parameters
    ----------
    papers : list
        A list of :class:`.Paper`
    features : dict
        Contains dictionary `{ type: { i: [ (f, w) ] } }` where `i` is an index
        for papers (see kwarg `index_by`), `f` is a feature (e.g. an N-gram), 
        and `w` is a weight on that feature (e.g. a count).
    index_by : str
        A key in :class:`.Paper` for indexing. If `features` is provided, then
        this must by the field from which indices `i` are drawn. For example, if
        a dictionary in `features` describes DfR wordcounts for the 
        :class:`.Paper`\s in `data`, and is indexed by DOI, then `index_by`
        should be 'doi'.
    exclude_features : set
        (optional) Features to ignore, e.g. stopwords.
        
    Returns
    -------
    :class:`.DataCollection`
    """
    
    def __init__(self, papers, features=None, index_by='wosid',
                                              index_citation_by='ayjid',
                                              exclude_features=set([])):
        
        self.papers = {}             # { p : paper }, where p is index_by
        self.features = {}
        self.authors = {}
        self.citations = {}          # { c : citation }
        self.papers_citing = {}      # { c : [ p ] }
        
        self.axes = {}
        self.index_by = index_by    # Field in Paper, e.g. 'wosid', 'doi'.
        self.index_citation_by = index_citation_by
    
        self.index(papers, features, index_by, index_citation_by, exclude_features)
        
    def index(self, papers, features, index_by, index_citation_by, exclude_features):

        # Check if data is a list of Papers.
        if type(papers) is not list or type(papers[0]) is not Paper:
            raise(ValueError("papers must be a list of Paper objects."))
        
        # Check if index_by is a valid key.
        self.datakeys = papers[0].keys()
        if index_by not in self.datakeys:
            raise(KeyError(str(index_by) + " not a valid key in data."))
    
        # Tokenize and index citations (both directions).
        self._index_citations(papers)
    
        # Index the Papers in data.
        for paper in papers:
            self.papers[paper[index_by]] = paper
        self.N_p = len(self.papers)
        
        # Index the Papers by author.
        self._index_papers_by_author()
        
        # Tokenize and index features.
        if features is not None:
            self._tokenize_features(features, exclude_features=exclude_features)
        else:
            logger.debug('features is None, skipping tokenization.')
            pass
        
    def _define_features(self, name, index, features, counts, documentCounts):
        self.features[name] = { 'index': index,
                                'features': features,
                                'counts': counts,
                                'documentCounts': documentCounts }

    def _index_papers_by_author(self):
        """
        Generates dict `{ author : [ p ] }` where `p` is an index of a
        :class:`.Paper` .
        """
        
        logger.debug('indexing authors in {0} papers'.format(self.N_p))
        
        author_dict = {}
        
        for k,p in self.papers.iteritems():
            for author in p.authors():
                if author in author_dict:
                    author_dict[author].append(k)
                else:
                    author_dict[author] = [k]
    
        self.N_a = len(author_dict)
        for k,v in author_dict.iteritems():
            self.authors[k] = v
        logger.debug('indexed {0} authors'.format(self.N_a))
    
    def _index_citations(self, papers):
        """
        Generates dict `{ c : citation }` and `{ c : [ p ] }`.
        """
        
        logger.debug('indexing citations in {0} papers'.format(len(papers)))
        
        cited = {}  # { p : [ (c,1) ] }
        citation_counts = Counter()
        citation_index = {}
        citation_index_ = {}
        
        citations = {}
        
        papers_citing = {}

        for paper in papers:
            p = paper[self.index_by]
            if paper['citations'] is not None:
                cited[p] = []

                for citation in paper['citations']:
                    c = citation[self.index_citation_by]
                    try:
                        c_i = citation_index_[c]
                    except KeyError:
                        c_i = len(citation_index)
                        citation_index[c_i] = c
                        citation_index_[c] = c_i

                    cited[p].append((c_i,1))
                    citation_counts[c_i] += 1

                    if c not in citations:
                        citations[c] = citation
                    
                    if c not in papers_citing:
                        papers_citing[c] = [ p ]
                    else:
                        papers_citing[c].append(p)
    
        # Separating this part allows for more flexibility in what sits behind
        #  self.papers_citing (e.g. HDF5 VArray).
        for k,v in papers_citing.iteritems():
            self.papers_citing[k] = v

        for k,v in citations.iteritems():
            self.citations[k] = v
    
        self._define_features('citations', citation_index, cited,
                                citation_counts, citation_counts)

        self.N_c = len(self.citations)
        logger.debug('indexed {0} citations'.format(self.N_c))

    def _tokenize_features(self, features, exclude_features=set([])):
        """
        
        Parameters
        ----------
        features : dict
            Contains dictionary `{ type: { i: [ (f, w) ] } }` where `i` is an 
            index for papers (see kwarg `index_by`), `f` is a feature (e.g. an
            N-gram), and `w` is a weight on that feature (e.g. a count).
        exclude_features : set
            (optional) Features to ignore, e.g. stopwords.
        """
        logger.debug('tokenizing {0} sets of features'.format(len(features)))
        
        def _handle(tok,w):
            if tok in findex_:
                counts[findex_[tok]] += w
                return True
            return False
        
        for ftype, fdict in features.iteritems():   # e.g. unigrams, bigrams
            logger.debug('tokenizing features of type {0}'.format(ftype))

            features = {}
            index = {}
            counts = Counter()
            documentCounts = Counter()
            
            # List of unique tokens.
            ftokenset = set([f for fval in fdict.values() for f,v in fval])
            ftokens = list(ftokenset - exclude_features)     # e.g. stopwords.
            logger.debug('found {0} unique tokens'.format(len(ftokens)))

            # Create forward and reverse indices.
            findex = { i:ftokens[i] for i in xrange(len(ftokens)) }
            findex_ = { v:k for k,v in findex.iteritems() }     # lookup.
            
            # Tokenize.
            for key, fval in fdict.iteritems(): # fval is a list of tuples.
                if type(fval) is not list or type(fval[0]) is not tuple:
                    raise ValueError('Malformed features data.')

                tokenized = [ (findex_[f],w) for f,w in fval if _handle(f,w) ]
                features[key] = tokenized
                for t,w in tokenized:
                    documentCounts[t] += 1
        
            self._define_features(ftype, findex, features, counts, documentCounts)
            
        logger.debug('done indexing features')
        
    def abstract_to_features(self,remove_stopwords=True):
        """
        Generates a set of unigram features from the abstracts of Papers.
        
        Automatically tokenizes and updates the :class:`.DataCollection`\.
        
        Parameters
        ----------
        remove_stopwords : bool
            (default: True) If True, passes tokenizer the NLTK stoplist.
        """

        unigrams = {}
        for p,paper in self.papers.iteritems():
            if paper['abstract'] is not None:
                term_counts = Counter()
                terms = strip_punctuation(paper['abstract'].lower()).split()
                for term in terms: term_counts[term] += 1
                unigrams[p] = term_counts.items()
                
        stoplist = set(stopwords.words())
                
        self._tokenize_features({'abstractTerms':unigrams}, stoplist)

        return unigrams
    
    def slice(self, key, method=None, **kwargs):
        """
        Slices data by key, using method (if applicable).

        Methods available for slicing a :class:`.DataCollection`\:

        ===========    =============================    =======    =============
        Method         Description                      Key        kwargs
        ===========    =============================    =======    =============
        time_window    Slices data using a sliding      date       window_size
                       time-window. Dataslices are                 step_size
                       indexed by the start of the 
                       time-window.
        time_period    Slices data into time periods    date       window_size
                       of equal length. Dataslices
                       are indexed by the start of
                       the time period.
        ===========    =============================    =======    =============


        The main difference between the sliding time-window (``time_window``) 
        and the time-period (``time_period``) slicing methods are whether the
        resulting periods can overlap. Whereas time-period slicing divides data
        into subsets by sequential non-overlapping time periods, subsets 
        generated by time-window slicing can overlap.

        .. figure:: _static/images/bibliocoupling/timeline.timeslice.png
           :width: 400
           :align: center
           
           **Time-period** slicing, with a window-size of 4 years.
           
        .. figure:: _static/images/bibliocoupling/timeline.timewindow.png
           :width: 400
           :align: center
           
           **Time-window** slicing, with a window-size of 4 years and a 
           step-size of 1 year.

        Avilable kwargs:

        ===========    ======   ================================================
        Argument       Type     Description
        ===========    ======   ================================================
        window_size    int      Size of time-window or period, in years
                                (default = 1).
        step_size      int      Amount to advance time-window or period in each
                                step (ignored for time_period).
        cumulative     bool     If True, the data from each successive slice 
                                includes the data from all preceding slices.
                                Only applies if key is 'date' (default = False).
        ===========    ======   ================================================ 
        
        Parameters
        ----------
        key : str
            key in :class:`.Paper` by which to slice data.
        method : str (optional)
            Dictates how data should be sliced. See table for available methods.
            If key is 'date', default method is time_period with window_size and
            step_size of 1.
        kwargs : kwargs
            See methods table, above.

        """
        if key == 'date':
            if method == 'time_window':
                kw = {  'window_size': kwargs.get('window_size', 1),
                        'step_size': kwargs.get('step_size', 1) }
                self.axes[key] = self._time_slice(**kw)

            elif method == 'time_period' or method is None:
                kw = {  'window_size': kwargs.get('window_size', 1),
                        'step_size': kwargs.get('window_size', 1),
                        'cumulative': kwargs.get('cumulative', False) }

                self.axes[key] = self._time_slice(**kw)
            else:
                raise(ValueError(str(method) + " not a valid slicing method."))
            
        # TODO: consider removing this, and just focusing on time.
        elif key == 'author':   # Already indexed.
            self.axes[key] = self.authors     # { a : [ p ] }

        # TODO: consider indexing journals in __init__, perhaps optionally.
        elif key in self.datakeys: # e.g. 'jtitle'
            self.axes[key] = {}     # { jtitle : [ p ] }
            for p,paper in self.papers.iteritems():
                try:
                    self.axes[key][paper[key]].append(p)
                except KeyError:
                    self.axes[key][paper[key]] = [p]
        else:
            raise(KeyError(str(key) + " not a valid key in data."))
        
    def _time_slice(self, **kwargs):
        """
        Slices data by date.

        If step_size = 1, this is a sliding time-window. If step_size =
        window_size, this is a time period slice.
        
        Parameters
        ----------
        kwargs : kwargs
            See table, below.
            
        Returns
        -------
        slices : dict
            Keys are start date of time slice, values are :class:`.Paper`
            indices (controlled by index_by argument in 
            :func:`.DataCollection.__init__` )
        
        Notes
        -----
        
        Avilable kwargs:

        ===========    ======   ================================================
        Argument       Type     Description
        ===========    ======   ================================================
        window_size    int      Size of time-window or period, in years
                                (default = 1).
        step_size      int      Amount to advance time-window or period in each
                                step (ignored for time_period).
        cumulative     bool     If True, the data from each successive slice 
                                includes the data from all preceding slices.
                                Only applies if key is 'date' (default = False).                                
        ===========    ======   ================================================           
        
        """

        # Get parameters from kwargs.
        window_size = kwargs.get('window_size', 1)
        step_size = kwargs.get('step_size', 1)
        start = kwargs.get('start', min([ paper['date']
                                           for paper in self.papers.values() ]))
        end = kwargs.get('start', max([ paper['date']
                                           for paper in self.papers.values() ]))
        cumulative = kwargs.get('cumulative', False)

        slices = {}     # { s : [ p ] }
        last = None
        for s in xrange(start, end-window_size+2, step_size):
            slices[s] = [ p for p,paper in self.papers.iteritems()
                            if s <= paper['date'] < s + window_size ]
            if cumulative and last is not None:
                slices[s] += last
            last = slices[s]
        return slices
        
    def indices(self):
        """
        Yields a list of indices of all papers in this :class:`.DataCollection`
        
        Returns
        -------
        list
            List of indices.
        """
        
        return self.papers.keys()
    
    def all_papers(self):
        """
        Yield the complete set of :class:`.Paper` instances in this
        :class:`.DataCollection` .
        
        Returns
        -------
        papers : list
            A list of :class:`.Paper`
        """
        
        return self.papers.values()
    
    def get_slices(self, key, include_papers=False):
        """
        Yields slices { k : [ p ] } for key.
        
        Parameters
        ----------
        key : str
            Key from :class:`.Paper` that has previously been used to slice data
            in this :class:`.DataCollection` .
        include_papers : bool
            If True, retrives :class:`.Paper` objects, rather than just indices.
        
        Returns
        -------
        slices : dict
            Keys are slice indices. If `include_papers` is `True`, values are 
            lists of :class:`.Paper` instances; otherwise returns paper indices 
            (e.g. 'wosid' or 'doi').

        Raises
        ------
        RuntimeError : DataCollection has not been sliced.
        KeyError : Data has not been sliced by [key] 
                    
        """

        if len(self.axes) == 0:
            raise(RuntimeError("DataCollection has not been sliced."))        
        if key not in self.axes.keys():
            raise(KeyError("Data has not been sliced by " + str(key)))
        
        slices = self.axes[key]

        if include_papers:  # Retrieve Papers.
            return { k:[ self.papers[p] for p in v ]
                     for k,v in slices.iteritems() }
        return slices

    def get_slice(self, key, index, include_papers=False):
        """
        Yields a specific slice.
        
        Parameters
        ----------
        key : str
            Key from :class:`.Paper` that has previously been used to slice data
            in this :class:`.DataCollection` .
        index : str or int
            Slice index for key (e.g. 1999 for 'date').
        include_papers : bool
            If True, retrives :class:`.Paper` objects, rather than just indices.
        
        Returns
        -------
        slice : list
            List of paper indices in this :class:`.DataCollection` , or (if
            `include_papers` is `True`) a list of :class:`.Paper` instances.

        Raises
        ------
        RuntimeError : DataCollection has not been sliced.
        KeyError : Data has not been sliced by [key] 
        KeyError : [index] not a valid index for [key]
        
        """

        if len(self.axes) == 0:
            raise(RuntimeError("DataCollection has not been sliced."))        
        if key not in self.axes.keys():
            raise(KeyError("Data has not been sliced by " + str(key)))
        if index not in self.axes[key].keys():
            raise(KeyError(str(index) + " not a valid index for " + str(key)))
        
        slice = self.axes[key][index]
        
        if include_papers:
            return [ self.papers[p] for p in slice ]
        return slice
    
    def get_by(self, key_indices, include_papers=False):
        """
        Given a set of (key, index) tuples, return the corresponding subset of
        :class:`.Paper` indices (or :class:`.Paper` instances themselves, if 
        papers is True).
        
        Parameters
        ----------
        key_indices : list
            A list of (key, index) tuples.
        include_papers : bool
            If True, retrives :class:`.Paper` objects, rather than just indices.
        
        Returns
        -------
        plist : list
            A list of paper indices, or :class:`.Paper` instances.
        
        Raises
        ------
        RuntimeError : DataCollection has not been sliced.

        """

        if len(self.axes) == 0:
            raise(RuntimeError("DataCollection has not been sliced."))
        
        slices = []
        for k,i in key_indices:
            slice = set(self.get_slice(k,i))
            slices.append(slice)

        plist = list( set.intersection(*slices) )
        
        if papers:
            return [ self.papers[s] for s in plist ]
        return plist
    
    def _get_slice_i(self, key, i):
        return self.axes[key].values()[i]
        
    def _get_by_i(self, key_indices):
        slices = []
        for k, i in key_indices:
            slice = set(self._get_slice_i(k, i))
            slices.append(slice)
        
        return list( set.intersection(*slices) )
    
    def _get_slice_keys(self, slice):
        if slice in self.get_axes():
            return self.axes[slice].keys()
    
    def get_axes(self):
        """
        Returns a list of all slice axes for this :class:`.DataCollection` .
        """
        
        return self.axes.keys()
    
    def N_axes(self):
        """
        Returns the number of slice axes for this :class:`.DataCollection` .
        """
        
        return len(self.axes.keys())
            
    def distribution(self, x_axis, y_axis=None):
        """
        Returns a Numpy array describing the number of :class:`.Paper`
        associated with each slice-coordinate, for x and y axes spcified.

        Returns
        -------
        dist : Numpy array
            A 2-dimensional array. Values are the number of
            :class:`.Paper` at that slice-coordinate.
            
        Raises
        ------
        RuntimeError : DataCollection has not been sliced.
        KeyError: Invalid slice axes for this DataCollection.
        """
        logger.debug('generate distribution over slices')
        if len(self.axes) == 0:
            logger.debug('datacollection has not been sliced')
            raise(RuntimeError("DataCollection has not been sliced."))
        if x_axis not in self.get_axes():
            logger.debug('datacollection has no axis {0}'.format(x_axis))
            raise(KeyError("X axis invalid for this DataCollection."))

        x_size = len(self.axes[x_axis])
        logger.debug('x axis size: {0}'.format(x_axis))

        if y_axis is not None:
            logger.debug('y axis: {0}'.format(y_axis))
            if y_axis not in self.get_axes():
                logger.debug('datacollection has not axis {0}'.format(y_axis))
                raise(KeyError("Y axis invalid for this DataCollection."))
            y_size = len(self.axes[y_axis])
            logger.debug('y axis size: {0}'.format(y_axis))
        else:   # Only 1 slice axis.
            logger.debug('only 1 slice axis')
            y_size = 1
        shape = (x_size, y_size)
        logger.debug('distribution shape: {0}'.format(shape))
        I = []
        J = []
        K = []
        for i in xrange(x_size):
            if y_axis is None:
                k = len(self._get_by_i([(x_axis, i)]))
                if k > 0:
                    I.append(i)
                    J.append(0)
                    K.append(k)
            else:
                for j in xrange(y_size):
                    k = len(self._get_by_i([(x_axis, i),(y_axis, j)]))
                    if k > 0:
                        I.append(i)
                        J.append(j)
                        K.append(k)

        # TODO: Move away from SciPy, to facilitate PyPy compatibility.
        dist = sc.sparse.coo_matrix((K, (I,J)), shape=shape)
        return dist

    def distribution_2d(self, x_axis, y_axis=None):
        """
        Deprecated as of 0.4.3-alpha. Use :func:`.distribution` instead.
        """

        return distribution(self, x_axis, y_axis=y_axis)

    def plot_distribution(self, x_axis=None, y_axis=None, type='bar', fig=None,
                                                                      **kwargs):
        """
        Plot distribution along slice axes, using MatPlotLib.
        
        Parameters
        ----------
        x_axis : str
        y_axis : str
            (optional)
        type : str
            'plot' or 'bar'
        **kwargs
            Passed to PyPlot method.
        """
        
        if fig is None:
            fig = plt.figure()
        
        if x_axis is None:
            x_axis = self.get_axes()[0]

        xkeys = self._get_slice_keys(x_axis)        
        
        if y_axis is None:
            plt.__dict__[type](xkeys, self.distribution(x_axis).todense(),
                                                                       **kwargs)
            plt.xlim(min(xkeys), max(xkeys))
        else:
            ykeys = self._get_slice_keys(y_axis)    
            ax = fig.add_subplot(111)
            ax.imshow(self.distribution(x_axis, y_axis).todense(), aspect=0.5,
                                                                       **kwargs)
            plt.yticks(np.arange(len(xkeys)), xkeys)

            tickstops = range(0, 50, int(50./len(plt.xticks()[0])))
            ax.set_xticks(tickstops)
            ax.set_xticklabels([ ykeys[i] for i in tickstops ])

#################################################
#       HDF5-related classes and methods        #
#################################################

pytype = {  tables.atom.BoolAtom: bool,
            tables.atom.UInt8Atom: int,
            tables.atom.Int16Atom: int,
            tables.atom.UInt16Atom: int,
            tables.atom.Int32Atom: int,
            tables.atom.UInt32Atom: long,
            tables.atom.Int64Atom: long,
            tables.atom.UInt64Atom: long,
            tables.atom.Float32Atom: float,
            tables.atom.Float64Atom: float,
            tables.atom.Complex64Atom: complex,
            tables.atom.Complex128Atom: complex,
            tables.atom.StringAtom: str,
            tables.atom.Time32Atom: int,
            tables.atom.Time64Atom: float }

class Index(tables.IsDescription):
    i = tables.Int32Col()
    mindex = tables.StringCol(100)

class papers_table(dict):
    """
    Mimics the `papers` dict in :class:`.Paper`\, providing HDF5 persistence.

    Values should be set only once for a key.

    Parameters
    ----------
    h5file : tables.file.File
        A :class:`tables.file.File` object.
    index_by : str
        Key in :class:`.Paper` used to index papers in this 
        :class:`.DataCollection`\.
    """
    def __init__(self, h5file, index_by, name):
        self.h5file = h5file
        self.index_by = index_by
        self.group = self.h5file.createGroup("/", name)
        self.table = self.h5file.createTable(self.group, 'papers_table',
                                                                      HDF5Paper)
        self.indexrows = self.table.cols.mindex.createIndex()
        
    def _to_paper(self, hdf5paper):
        """
        Yields a :class:`.Paper` from an :class:`.HDF5Paper`\.
        """
        paper = Paper()
        keys = self.table.description._v_dtypes.keys()
        for kname in keys:
            if kname == 'mindex': continue    # Not a valid field for Paper.
            v = hdf5paper[kname]
            
            if type(v) is numpy.int32:
                v = int(v)
            elif type(v) is numpy.string_:
                v = str(v)
            
            # Look for lists, and recast.
            if type(v) is str:
                if len(v) > 0:

                    if v[0] == '[' and v[-1] == ']':
                        v = [ s.strip().replace("'","")
                                for s in v[1:-1].split(',') ]

            paper[kname] = v
        return paper
        
    def __setitem__(self, key, value):
        hpaper = self.table.row
        hpaper['mindex'] = key

        for k, v in value.iteritems():
            if k in self.table.cols.__dict__:
                if v is not None:
                    if type(v) is list:
                        v = str(v)
                    hpaper[k] = v
        hpaper.append()
        self.table.flush() 
    
    def __getitem__(self, key):
        return [ self._to_paper(x) for x
                    in self.table.where('mindex == b"{0}"'.format(key)) ][0]
    
    def __len__(self):
        return len(self.table)
    
    def __contains__(self, key):
        size = len([ self._to_paper(x) for x
                        in self.table.where('mindex == b"{0}"'.format(key)) ])
        return size > 0
    
    def iteritems(self):
        i = 0
        while i < len(self.table):
            x = self.table[i]
            yield x['mindex'],self._to_paper(x)
            i += 1
            
    def values(self):
        return [ self._to_paper(x) for x in self.table ]
    
    def keys(self):
        return [ x['mindex'] for x in self.table ]

class vlarray_dict(dict):
    """
    Provides dict-like access to an HDF5 VLArray.
    """
    def __init__(self, h5file, group, name, atom):
        self.h5file = h5file
        self.group = group
        self.name = name
        self.atom = atom
        
        try:
            self.pytype = pytype[type(atom)]
        except KeyError:
            raise NotImplementedError('No equivalent Python type for atom.')
        
        self.index = self.h5file.createTable(self.group,
                                                '{0}_index'.format(self.name),
                                                Index)
        self.vlarray = self.h5file.createVLArray(self.group, self.name,
                                                             self.atom)

        self.index.cols.mindex.createIndex()        
        
    def __setitem__(self, key, value):
#            i = int([ x['i'] for x in self.index.where('(mindex == b"{0}")'.format(key)) ][0])
#            self.vlarray[i] = value
        if key not in self:
            i = len(self.vlarray)
            ind = self.index.row
            ind['i'] = i
            ind['mindex'] = key
            ind.append()
            self.index.flush()
        
        self.vlarray.append(value)
        self.vlarray.flush()

    def __getitem__(self, key):
        rset = [ x['i'] for x
                    in self.index.where('(mindex == b"{0}")'.format(key)) ]

        i = int(rset[0])
        return [ self.pytype(v) for v in self.vlarray[i] ]

    def __contains__(self,key):
        size = len([ self._to_paper(x) for x
                    in self.index.where('mindex == b"{0}"'.format(key)) ])
        return size > 0
    
    def values(self):
        return [ [ self.pytype(v) for v in x ] for x in self.vlarray ]
    
    def keys(self):
        return [ x['mindex'] for x in self.index ]  
    
    def __len__(self):
        return len(self.vlarray)

class HDF5DataCollection(DataCollection):
    def __init__(self, papers, features=None, index_by='wosid',
                                              index_citation_by='ayjid',
                                              exclude_features=set([]),
                                              datapath=None):
        # Where to save the HDF5 data file?
        if datapath is None:
            self.datapath = tempfile.mkdtemp()
        else:
            self.datapath = datapath
            
        self.uuid = uuid.uuid4()    # Unique identifier for this DataCollection.
        self.path = '{0}/DataCollection-{1}.h5'.format(self.datapath, self.uuid)
        self.h5file = tables.openFile(self.path, mode = "w",
                                   title='DataCollection-{0}'.format(self.uuid))
        self.group = self.h5file.createGroup("/", 'arrays')
        
        self.papers = papers_table(self.h5file, index_by, 'papers')
        
        
        
        self.features = {}  # TODO: implement the HDF5 features group/tables.
        self.authors = vlarray_dict(self.h5file, self.group, 'authors',
                                                 tables.StringAtom(100))
        self.citations = papers_table(self.h5file, index_citation_by,
                                                   'citations')
        self.papers_citing = vlarray_dict(self.h5file, self.group,
                                                       'papers_citing',
                                                       tables.StringAtom(100))
        
        self.axes = {}
        self.index_by = index_by    # Field in Paper, e.g. 'wosid', 'doi'.
        self.index_citation_by = index_citation_by        
        
        self.index(papers, features, index_by, index_citation_by,
                                               exclude_features)