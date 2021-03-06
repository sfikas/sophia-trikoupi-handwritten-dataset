'''
Created on Apr, 2019
@author: sfikas
'''
import os
import numpy as np
from skimage import io as img_io
from sklearn.preprocessing import LabelEncoder
import torch
from torch.utils.data import Dataset

from skimage.transform import resize

from cnn_ws.io.list_io import LineListIO
from decoder_src.phoc import PhocLayout
from cnn_ws.transformations.image_size import check_size
from cnn_ws.transformations.homography_augmentation import HomographyAugmentation
from .check_sophia import get_words_from_pagexml

import tqdm
import warnings

class SophiaDataset(Dataset):
    '''
    Note/warning:
    Instead, the criterion to label a word as a query is unique_word_strings[np.where(counts > 1)[0]].
    '''
    def __init__(self, phoc_layout: PhocLayout,
                root_dir = 'data/',
                embedding='phoc',
                min_image_width_height=30,
                fixed_image_size=None,
                max_wordlength=20,
                ):
        '''
        We need to fill in:
        self.words                          list of tuples: (word_img, transcr, page_id). word_img is an intensity matrix, transcr is a string with the transcription, page_id holds word info (optional?)
        self.split_ids                      list of ids: tag each word with a partition label (here, training=1, validation=2, test=3)
        self.word_embeddings                list of targets that correspond to the words (PHOC embeddings or word lengths)

        To be filled-in automatically:
        self.label_encoder                  compute a mapping from class string to class id. Initialize after filling-in self.words.        
        self.query_list                     this is defined in MainLoader.
        '''
        def xml2jpg(xml):
            base, ext = os.path.splitext(xml)
            return base + '.JPG'
        self.TRAINING_PARTITION     = 1
        self.VALIDATION_PARTITION   = 2
        self.TEST_PARTITION         = 3
        if embedding not in ['phoc', 'wordlength']:
            raise ValueError('embedding must be either phoc or wordlength')

        self.word_list = None
        self.word_string_embeddings = None
        self.query_list = None
        self.label_encoder = None
        self.fixed_image_size = fixed_image_size

        # Specify images of the set
        all_xmls = []
        for x in range(1, 48):
            if x == 12:
                continue #Page 12 was omitted / doesn't exist
            all_xmls.append(os.path.join(root_dir, '_00{0:02d}.xml'.format(x)) )
        # load the dataset
        self.words = []
        self.split_ids = []
        word_id = 1
        for page_id in all_xmls:
            doc_img = img_io.imread(xml2jpg(page_id))
            doc_img = np.mean(doc_img, axis=2)               # inputs of sophia are colour
            doc_img = 1 - doc_img.astype(np.float32) / 255.0 # scale black pixels to 1 and white pixels to 0
            for word in get_words_from_pagexml(page_id):
                x, y, w, h = word[1]
                word_img = doc_img[y:y+h, x:x+w].copy()
                word_img = check_size(img=word_img, min_image_width_height=min_image_width_height)
                # Decide on split_id (this comes from footnote on page 3 of Sfikas et al.2015)
                if word_id >= 1 and word_id <= 2000:
                    current_split_id = self.TRAINING_PARTITION
                elif word_id >= 2001 and word_id <= 4000:
                    current_split_id = self.TEST_PARTITION
                elif word_id >= 4001 and word_id <= 4941:
                    current_split_id = self.VALIDATION_PARTITION
                else:
                    raise ValueError('Word id read out of bounds (={}); it should have been in [1,4941].'.format(current_split_id))
                transcr = word[2]
                self.words.append((word_img, transcr, page_id))
                self.split_ids.append(current_split_id)
                word_id += 1

        self.label_encoder = LabelEncoder()
        word_strings = [elem[1] for elem in self.words]
        self.label_encoder.fit(word_strings)

        self.word_embeddings = None
        if embedding == 'phoc':
            self.word_embeddings = phoc_layout.build_phoc_descriptor(word_strings)
        elif embedding == 'wordlength':
            self.word_embeddings = []
            for x in word_strings:
                tt = np.zeros([max_wordlength,])
                try:
                    tt[len(x) - 1] = 1
                except IndexError:
                    print('Word length (for word "{}") over max word length ({})'.format(x, max_wordlength))
                    exit(1)
                self.word_embeddings.append(tt)
            self.word_embeddings = np.array(self.word_embeddings)
        else:
            raise NotImplementedError()
        self.word_embeddings = self.word_embeddings.astype(np.float32)


    def mainLoader(self, partition=None, transforms=HomographyAugmentation()):
        self.transforms = transforms
        self.word_list = []
        self.word_string_embeddings = []
        if partition not in [None, 'train', 'test', 'validation']:
            raise ValueError('partition must be one of None, train, test, validation')

        if partition is not None:
            if partition == 'train':
                partition_id = self.TRAINING_PARTITION
            elif partition == 'test':
                partition_id = self.TEST_PARTITION #self.TEST_PARTITION
            elif partition == 'validation':
                partition_id = self.VALIDATION_PARTITION
            else:
                raise NotImplementedError('This partition type is not used in the current implementation.')
            for word, string, split_id in zip(self.words, self.word_embeddings, self.split_ids):
                if(len(word[1]) == 0):
                    #print('Skipped empty word (probably contained a single special character)')
                    continue
                if(split_id == partition_id):
                    self.word_list.append(word)
                    self.word_string_embeddings.append(string)
        else:
            self.word_list = self.words
            self.word_string_embeddings = self.word_embeddings


        if partition == 'test':
            word_strings = [elem[1] for elem in self.word_list]
            unique_word_strings, counts = np.unique(word_strings, return_counts=True)
            qry_word_ids = unique_word_strings[np.where(counts > 1)[0]]

            query_list = np.zeros(len(word_strings), np.int8)
            qry_ids = [i for i in range(len(word_strings)) if word_strings[i] in qry_word_ids]
            query_list[qry_ids] = 1

            self.query_list = query_list
        else:
            word_strings = [elem[1] for elem in self.word_list]
            self.query_list = np.zeros(len(word_strings), np.int8)

        if partition == 'train':
            word_strings = [elem[1] for elem in self.word_list]
            unique_word_strings, counts = np.unique(word_strings, return_counts=True)
            ref_count_strings = {uword : count for uword, count in zip(unique_word_strings, counts)}
            weights = [1.0/ref_count_strings[word] for word in word_strings]
            self.weights = np.array(weights)/sum(weights)


    def embedding_size(self):
        return len(self.word_string_embeddings[0])


    def __len__(self):
        return len(self.word_list)

    def __getitem__(self, index):
        word_img = self.word_list[index][0]
        transcription = self.word_list[index][1]
        if self.transforms is not None:
            word_img = self.transforms(word_img)

        # fixed size image !!!
        word_img = self._image_resize(word_img, self.fixed_image_size)

        word_img = word_img.reshape((1,) + word_img.shape)
        word_img = torch.from_numpy(word_img)
        embedding = self.word_string_embeddings[index]
        embedding = torch.from_numpy(embedding)
        class_id = self.label_encoder.transform([self.word_list[index][1]])
        is_query = self.query_list[index]

        return word_img, embedding, class_id, is_query, transcription

    # fixed sized image
    @staticmethod
    def _image_resize(word_img, fixed_img_size):
        if fixed_img_size is not None:
            if len(fixed_img_size) == 1:
                scale = float(fixed_img_size[0]) / float(word_img.shape[0])
                new_shape = (int(scale * word_img.shape[0]), int(scale * word_img.shape[1]))
            if len(fixed_img_size) == 2:
                new_shape = (fixed_img_size[0], fixed_img_size[1])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                word_img = resize(image=word_img, output_shape=new_shape, mode='constant').astype(np.float32)
        return word_img