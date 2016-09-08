from timeit import default_timer as timer
import pandas as pd
import numpy as np
import scipy as sp
import scipy.sparse
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import svds
from polara.recommender import data, defaults
from polara.recommender.evaluation import get_hits, get_relevance_scores, get_ranking_scores
from polara.recommender.utils import array_split
from polara.lib.hosvd import tucker_als

class RecommenderModel(object):
    _config = ('topk', 'filter_seen', 'switch_positive', 'predict_negative')
    _pad_const = -1 # used for sparse data

    def __init__(self, recommender_data):

        self.data = recommender_data
        self._recommendations = None
        self.method = 'ABC'

        self._topk = defaults.get_config(['topk'])['topk']
        self.filter_seen  = defaults.get_config(['filter_seen'])['filter_seen']
        self.switch_positive  = defaults.get_config(['switch_positive'])['switch_positive']
        self.verify_integrity =  defaults.get_config(['verify_integrity'])['verify_integrity']


    @property
    def recommendations(self):
        if (self._recommendations is None):
            try:
                self._recommendations = self.get_recommendations()
            except AttributeError:
                print '{} model is not ready. Rebuilding.'.format(self.method)
                self.build()
                self._recommendations = self.get_recommendations()
        return self._recommendations


    @property
    def topk(self):
        return self._topk

    @topk.setter
    def topk(self, new_value):
        #support rolling back scenarion for @k calculations
        if (self._recommendations is not None) and (new_value > self._recommendations.shape[1]):
            self._recommendations = None #if topk is too high - recalculate recommendations
        self._topk = new_value


    def build(self):
        raise NotImplementedError('This must be implemented in subclasses')


    def get_recommendations(self):
        raise NotImplementedError('This must be implemented in subclasses')


    def get_matched_predictions(self):
        userid, itemid = self.data.fields.userid, self.data.fields.itemid
        holdout_data = self.data.test.evalset[itemid]
        holdout = self.data.holdout_size
        holdout_matrix = holdout_data.values.reshape(-1, holdout).astype(np.int64)

        recommendations = self.recommendations #will recalculate if empty

        if recommendations.shape[0] > holdout_matrix.shape[0]:
            print 'Evaluation set is truncated.'
            recommendations = recommendations[:holdout_matrix.shape[0], :]
        elif recommendations.shape[0] < holdout_matrix.shape[0]:
            print 'Recommendations are truncated.'
            holdout_matrix = holdout_matrix[:recommendations.shape[0], :]

        matched_predictions = (recommendations[:, :, None] == holdout_matrix[:, None, :])
        return matched_predictions


    def get_feedback_data(self):
        feedback = self.data.fields.feedback
        eval_data = self.data.test.evalset[feedback].values
        holdout = self.data.holdout_size
        feedback_data = eval_data.reshape(-1, holdout)
        return feedback_data


    def get_positive_feedback(self):
        feedback_data = self.get_feedback_data()
        positive_feedback = feedback_data >= self.switch_positive
        return positive_feedback


    def evaluate(self, method='hits', topk=None):
        #support rolling back scenario for @k calculations
        if topk > self.topk:
            self.topk = topk #will also empty flush old recommendations

        matched_predictions = self.get_matched_predictions()
        matched_predictions = matched_predictions[:, :topk, :]

        if method == 'relevance':
            positive_feedback = self.get_positive_feedback()
            scores = get_relevance_scores(matched_predictions, positive_feedback)
        elif method == 'ranking':
            feedback = self.get_feedback_data()
            scores = get_ranking_scores(matched_predictions, feedback, self.switch_positive)
        elif method == 'hits':
            positive_feedback = self.get_positive_feedback()
            scores = get_hits(matched_predictions, positive_feedback)
        else:
            raise NotImplementedError
        return scores


    @staticmethod
    def topsort(a, topk):
        parted = np.argpartition(a, -topk)[-topk:]
        return parted[np.argsort(-a[parted])]


    @staticmethod
    def downvote_seen_items(recs, idx_seen):
        # NOTE for sparse scores matrix this method can lead to a slightly worse
        # results (comparing to the same method but with "densified" scores matrix)
        # models with sparse scores can alleviate that by extending recommendations
        # list with most popular items or items generated by a more sophisticated logic
        idx_seen = idx_seen[:2] # need only users and items
        if sp.sparse.issparse(recs):
            # No need to create 2 idx sets form idx lists.
            # When creating a set have to iterate over list (O(n)).
            # Intersecting set with list gives the same O(n).
            # So there's no performance gain in converting large list into set!
            # Moreover, large set creates additional memory overhead. Hence,
            # need only to create set from the test idx and calc intersection.
            recs_idx = pd.lib.fast_zip(list(recs.nonzero())) #larger
            seen_idx = pd.lib.fast_zip(list(idx_seen)) #smaller
            idx_seen_bool = np.in1d(recs_idx, set(seen_idx))
            # sparse data may have no intersections with seen items
            if idx_seen_bool.any():
                seen_data = recs.data[idx_seen_bool]
                # move seen items scores below minimum value
                # if not enough data, seen items won't be filtered out
                lowered = recs.data.min() - (seen_data.max() - seen_data) - 1
                recs.data[idx_seen_bool] = lowered
        else:
            idx_seen_flat = np.ravel_multi_index(idx_seen, recs.shape)
            seen_data = recs.flat[idx_seen_flat]
            # move seen items scores below minimum value
            lowered = recs.min() - (seen_data.max() - seen_data) - 1
            recs.flat[idx_seen_flat] = lowered


    def get_topk_items(self, scores):
        topk = self.topk
        if sp.sparse.issparse(scores):
            # there can be less then topk values in some rows
            # need to extend sorted scores to conform with evaluation matrix shape
            # can do this by adding -1's to the right, however:
            # this relies on the fact that there are no -1's in evaluation matrix
            # NOTE need to ensure that this is always true
            def topscore(x, k):
                data = x.data.values
                cols = x.cols.values
                nnz = len(data)
                if k >= nnz:
                    cols_sorted = cols[np.argsort(-data)]
                    # need to pad values to conform with evaluation matrix shape
                    res = np.pad(cols_sorted, (0, k-nnz), 'constant', constant_values=self._pad_const)
                else:
                    # TODO verify, that even if k is relatively small, then
                    # argpartition doesn't add too much overhead?
                    res = cols[self.topsort(data, k)]
                return res

            idx = scores.nonzero()
            row_data = pd.DataFrame({'data': scores.data, 'cols': idx[1]}).groupby(idx[0], sort=True)
            recs = np.asarray(row_data.apply(topscore, topk).tolist())
        else:
        # apply_along_axis is more memory efficient then argsort on full array
            recs = np.apply_along_axis(self.topsort, 1, scores, topk)
        return recs


    @staticmethod
    def orthogonalize(u, v):
        Qu, Ru = np.linalg.qr(u)
        Qv, Rv = np.linalg.qr(v)
        Ur, Sr, Vr = np.linalg.svd(Ru.dot(Rv.T))
        U = Qu.dot(Ur)
        V = Qv.dot(Vr.T)
        return U, V


    def verify_data_integrity(self):
        data = self.data
        userid, itemid, feedback = data.fields

        nunique_items = data.training[itemid].nunique()
        nunique_test_users = data.test.testset[userid].nunique()

        assert nunique_items == len(data.index.itemid)
        assert nunique_items == data.training[itemid].max() + 1
        assert nunique_test_users == data.test.testset[userid].max() + 1

        try:
            assert self._items_factors.shape[0] == len(data.index.itemid)
            assert self._feedback_factors.shape[0] == len(data.index.feedback)
        except AttributeError:
            pass


class NonPersonalized(RecommenderModel):

    def __init__(self, kind, *args, **kwargs):
        super(NonPersonalized, self).__init__(*args, **kwargs)
        self.method = kind


    def build(self):
        self._recommendations = None


    def get_recommendations(self):
        userid, itemid, feedback = self.data.fields
        test_data = self.data.test.testset
        test_idx = (test_data[userid].values.astype(np.int64),
                    test_data[itemid].values.astype(np.int64))
        num_users = self.data.test.testset[userid].max() + 1

        if self.method == 'mostpopular':
            items_scores = self.data.training.groupby(itemid, sort=True).size().values
            #scores =  np.lib.stride_tricks.as_strided(items_scores, (num_users, items_scores.size), (0, items_scores.itemsize))
            scores = np.repeat(items_scores[None, :], num_users, axis=0)
        elif self.method == 'random':
            num_items = self.data.training[itemid].max() + 1
            scores = np.random.random((num_users, num_items))
        elif self.method == 'topscore':
            items_scores = self.data.training.groupby(itemid, sort=True)[feedback].sum().values
            scores = np.repeat(items_scores[None, :], num_users, axis=0)
        else:
            raise NotImplementedError

        if self.filter_seen:
            #prevent seen items from appearing in recommendations
            self.downvote_seen_items(scores, test_idx)

        top_recs =  self.get_topk_items(scores)
        return top_recs


class CooccurrenceModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(CooccurrenceModel, self).__init__(*args, **kwargs)
        self.method = 'item-to-item' #pick some meaningful name
        self.implicit = True


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo(tensor_mode=False)
        #np.ones_like makes feedback implicit
        if self.implicit:
            val = np.ones_like(val)
        user_item_matrix = sp.sparse.coo_matrix((val, (idx[:, 0], idx[:, 1])),
                                          shape=shp, dtype=np.float64).tocsr()

        tik = timer()
        i2i_matrix = user_item_matrix.T.dot(user_item_matrix)

        #exclude "self-links"
        diag_vals = i2i_matrix.diagonal()
        i2i_matrix -= sp.sparse.dia_matrix((diag_vals, 0), shape=i2i_matrix.shape)
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)

        self._i2i_matrix = i2i_matrix


    def get_recommendations(self):
        userid, itemid, feedback = self.data.fields
        test_data = self.data.test.testset
        i2i_matrix = self._i2i_matrix

        idx = (test_data[userid].values, test_data[itemid].values)
        val = test_data[feedback].values
        if self.implicit:
            val = np.ones_like(val)
        shp = (idx[0].max()+1, i2i_matrix.shape[0])
        test_matrix = sp.sparse.coo_matrix((val, idx), shape=shp,
                                           dtype=np.float64).tocsr()
        i2i_scores = test_matrix.dot(self._i2i_matrix)

        if self.filter_seen:
            # prevent seen items from appearing in recommendations;
            # caution: there's a risk of having seen items in the list
            # (for topk < i2i_matrix.shape[1]-len(unseen))
            # this is related to low generalization ability
            # of the naive cooccurrence method itself, not to the algorithm
            self.downvote_seen_items(i2i_scores, test_data)

        top_recs = self.get_topk_items(i2i_scores)
        return top_recs


class SVDModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(SVDModel, self).__init__(*args, **kwargs)
        self.rank = defaults.svd_rank
        self.method = 'SVD'


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo(tensor_mode=False)
        svd_matrix = sp.sparse.coo_matrix((val, (idx[:, 0], idx[:, 1])),
                                          shape=shp, dtype=np.float64).tocsr()

        tik = timer()
        _, _, items_factors = svds(svd_matrix, k=self.rank, return_singular_vectors='vh')
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)

        self._items_factors = np.ascontiguousarray(items_factors[::-1, :])


    def get_recommendations(self):
        userid, itemid, feedback = self.data.fields
        test_data = self.data.test.testset

        test_idx = (test_data[userid].values.astype(np.int64),
                    test_data[itemid].values.astype(np.int64))
        test_val = test_data[feedback].values

        v = self._items_factors
        test_shp = (test_data[userid].max()+1,
                    v.shape[1])

        test_matrix = sp.sparse.coo_matrix((test_val, test_idx),
                                           shape=test_shp,
                                           dtype=np.float64).tocsr()

        svd_scores = (test_matrix.dot(v.T)).dot(v)


        if self.filter_seen:
            #prevent seen items from appearing in recommendations
            self.downvote_seen_items(svd_scores, test_idx)

        top_recs = self.get_topk_items(svd_scores)
        return top_recs


class CoffeeModel(RecommenderModel):

    def __init__(self, *args, **kwargs):
        super(CoffeeModel, self).__init__(*args, **kwargs)
        self.mlrank = defaults.mlrank
        self.chunk = defaults.test_chunk_size
        self.method = 'CoFFee'
        self._flattener = defaults.flattener
        self.growth_tol = defaults.growth_tol
        self.num_iters = defaults.num_iters
        self.show_output = defaults.show_output


    @property
    def flattener(self):
        return self._flattener

    @flattener.setter
    def flattener(self, new_value):
        old_value = self._flattener
        if new_value != old_value:
            self._flattener = new_value
            self._recommendations = None


    @staticmethod
    def flatten_scores(tensor_scores, flattener=None):
        flattener = flattener or slice(None)
        if isinstance(flattener, str):
            slicer = slice(None)
            flatten = getattr(np, flattener)
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif isinstance(flattener, int):
            slicer = flattener
            matrix_scores = tensor_scores[:, :, slicer]
        elif isinstance(flattener, list) or isinstance(flattener, slice):
            slicer = flattener
            flatten = np.sum
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif isinstance(flattener, tuple):
            slicer, flatten_method = flattener
            slicer = slicer or slice(None)
            flatten = getattr(np, flatten_method)
            matrix_scores = flatten(tensor_scores[:, :, slicer], axis=-1)
        elif callable(flattener):
            matrix_scores = flattener(tensor_scores)
        else:
            raise ValueError('Unrecognized value for flattener attribute')
        return matrix_scores


    def build(self):
        self._recommendations = None
        idx, val, shp = self.data.to_coo(tensor_mode=True)
        tik = timer()
        users_factors, items_factors, feedback_factors, core = \
                            tucker_als(idx, val, shp, self.mlrank,
                            growth_tol=self.growth_tol,
                            iters = self.num_iters,
                            batch_run=not self.show_output)
        tok = timer() - tik
        print '{} model training time: {}s'.format(self.method, tok)
        self._users_factors = users_factors
        self._items_factors = items_factors
        self._feedback_factors = feedback_factors
        self._core = core


    def get_recommendations(self):
        userid, itemid, feedback = self.data.fields
        v = self._items_factors
        w = self._feedback_factors

        test_shp = (self.data.test.testset[userid].max()+1, v.shape[0], w.shape[0])
        user_idx = self.data.test.testset.loc[:, userid].values.astype(np.int64)
        item_idx = self.data.test.testset.loc[:, itemid].values.astype(np.int64)
        fdbk_idx = self.data.test.testset.loc[:, feedback].values

        fdbk_idx = self.data.index.feedback.set_index('old').loc[fdbk_idx, 'new'].values
        if np.isnan(fdbk_idx).any():
            raise NotImplementedError('Not all values of feedback are present in training data')
        else:
            fdbk_idx = fdbk_idx.astype(np.int64)

        idx_data = (user_idx, item_idx, fdbk_idx)
        idx_flat = np.ravel_multi_index(idx_data, test_shp)
        shp_flat = (test_shp[0]*test_shp[1], test_shp[2])
        idx = np.unravel_index(idx_flat, shp_flat)

        val = np.ones(self.data.test.testset.shape[0],)
        test_tensor_mat = sp.sparse.coo_matrix((val, idx), shape=shp_flat).tocsr()

        coffee_scores = np.empty((test_shp[0], test_shp[1]))
        chunk = self.chunk
        flattener = self.flattener
        for i in xrange(0, test_shp[0], chunk):
            start = i
            stop = min(i+chunk, test_shp[0])

            test_slice = test_tensor_mat[start*test_shp[1]:stop*test_shp[1], :]
            slice_scores = test_slice.dot(w).reshape(stop-start, test_shp[1], w.shape[1])
            slice_scores = np.tensordot(slice_scores, v, axes=(1, 0))
            slice_scores = np.tensordot(np.tensordot(slice_scores, v, axes=(2, 1)), w, axes=(1, 1))

            coffee_scores[start:stop, :] = self.flatten_scores(slice_scores, flattener)

        if self.filter_seen:
            #prevent seen items from appearing in recommendations
            self.downvote_seen_items(coffee_scores, idx_data[:2])

        top_recs = self.get_topk_items(coffee_scores)
        return top_recs
