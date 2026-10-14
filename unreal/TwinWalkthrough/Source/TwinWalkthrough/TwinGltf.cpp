#include "TwinGltf.h"

#include "Dom/JsonObject.h"
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

namespace
{
	constexpr uint32 GlbMagic = 0x46546C67;  // "glTF"
	constexpr uint32 ChunkJson = 0x4E4F534A;
	constexpr uint32 ChunkBin = 0x004E4942;
	constexpr int32 MaxTextureSize = 4096;

	struct FGlb
	{
		TSharedPtr<FJsonObject> Json;
		const uint8* Bin = nullptr;
		int64 BinSize = 0;
	};

	uint32 ReadU32(const uint8* P)
	{
		uint32 V;
		FMemory::Memcpy(&V, P, 4);
		return V;  // GLB is little-endian, like every platform this viewer ships on
	}

	bool ReadGlb(const TArray<uint8>& Data, FGlb& Out, FString& Err)
	{
		if (Data.Num() < 20 || ReadU32(Data.GetData()) != GlbMagic)
		{
			Err = TEXT("the model is not a glTF binary (GLB)");
			return false;
		}
		if (ReadU32(Data.GetData() + 4) != 2)
		{
			Err = FString::Printf(TEXT("unsupported glTF version %u (need 2)"), ReadU32(Data.GetData() + 4));
			return false;
		}
		if (ReadU32(Data.GetData() + 8) != (uint32)Data.Num())
		{
			Err = FString::Printf(TEXT("the model is truncated (%d of %u bytes)"), Data.Num(), ReadU32(Data.GetData() + 8));
			return false;
		}
		int64 Off = 12;
		while (Off + 8 <= Data.Num())
		{
			const uint32 Len = ReadU32(Data.GetData() + Off);
			const uint32 Type = ReadU32(Data.GetData() + Off + 4);
			const uint8* Ptr = Data.GetData() + Off + 8;
			if (Off + 8 + (int64)Len > Data.Num())
			{
				Err = TEXT("a GLB chunk runs past the end of the file");
				return false;
			}
			if (Type == ChunkJson)
			{
				const FUTF8ToTCHAR Conv(reinterpret_cast<const ANSICHAR*>(Ptr), Len);
				const FString Text(Conv.Length(), Conv.Get());
				const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Text);
				if (!FJsonSerializer::Deserialize(Reader, Out.Json) || !Out.Json.IsValid())
				{
					Err = TEXT("the GLB's JSON chunk is not valid JSON");
					return false;
				}
			}
			else if (Type == ChunkBin)
			{
				Out.Bin = Ptr;
				Out.BinSize = Len;
			}
			Off += 8 + Len;
		}
		if (!Out.Json.IsValid())
		{
			Err = TEXT("the GLB has no JSON chunk");
			return false;
		}
		return true;
	}

	const TArray<TSharedPtr<FJsonValue>>& Arr(const TSharedPtr<FJsonObject>& O, const TCHAR* Key)
	{
		static const TArray<TSharedPtr<FJsonValue>> Empty;
		const TArray<TSharedPtr<FJsonValue>>* A = nullptr;
		return (O.IsValid() && O->TryGetArrayField(Key, A)) ? *A : Empty;
	}

	TSharedPtr<FJsonObject> ObjAt(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, int32 Index)
	{
		const TArray<TSharedPtr<FJsonValue>>& A = Arr(O, Key);
		return A.IsValidIndex(Index) ? A[Index]->AsObject() : nullptr;
	}

	double Num(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, double Default)
	{
		double V = Default;
		return (O.IsValid() && O->TryGetNumberField(Key, V)) ? V : Default;
	}

	int32 Int(const TSharedPtr<FJsonObject>& O, const TCHAR* Key, int32 Default = INDEX_NONE)
	{
		return (int32)Num(O, Key, Default);
	}

	struct FView
	{
		const uint8* Data = nullptr;
		int64 Count = 0;
		int32 Comps = 0;
		int32 CompType = 0;
		int64 Stride = 0;
	};

	int32 CompSize(int32 CompType)
	{
		switch (CompType)
		{
		case 5120: case 5121: return 1;
		case 5122: case 5123: return 2;
		case 5125: case 5126: return 4;
		default: return 0;
		}
	}

	bool Accessor(const FGlb& G, int32 Index, FView& V, FString& Err)
	{
		const TSharedPtr<FJsonObject> A = ObjAt(G.Json, TEXT("accessors"), Index);
		if (!A.IsValid())
		{
			Err = FString::Printf(TEXT("accessor %d does not exist"), Index);
			return false;
		}
		const int32 ViewIndex = Int(A, TEXT("bufferView"));
		const TSharedPtr<FJsonObject> BV = ObjAt(G.Json, TEXT("bufferViews"), ViewIndex);
		if (!BV.IsValid() || A->HasField(TEXT("sparse")))
		{
			Err = FString::Printf(TEXT("accessor %d has no buffer view or is sparse (unsupported)"), Index);
			return false;
		}
		if (Int(BV, TEXT("buffer"), 0) != 0 || !G.Bin)
		{
			Err = TEXT("the model references an external buffer (only self-contained GLB files are supported)");
			return false;
		}
		const FString Type = A->GetStringField(TEXT("type"));
		V.Comps = Type == TEXT("SCALAR") ? 1 : Type == TEXT("VEC2") ? 2 : Type == TEXT("VEC3") ? 3 : Type == TEXT("VEC4") ? 4 : 0;
		V.CompType = Int(A, TEXT("componentType"), 0);
		V.Count = (int64)Num(A, TEXT("count"), 0);
		const int32 Elem = CompSize(V.CompType) * V.Comps;
		if (Elem == 0)
		{
			Err = FString::Printf(TEXT("accessor %d has an unsupported type %s/%d"), Index, *Type, V.CompType);
			return false;
		}
		V.Stride = (int64)Num(BV, TEXT("byteStride"), 0);
		if (V.Stride == 0)
		{
			V.Stride = Elem;
		}
		const int64 Start = (int64)Num(BV, TEXT("byteOffset"), 0) + (int64)Num(A, TEXT("byteOffset"), 0);
		if (V.Count < 0 || Start < 0 || (V.Count > 0 && Start + V.Stride * (V.Count - 1) + Elem > G.BinSize))
		{
			Err = FString::Printf(TEXT("accessor %d reads past the end of the binary chunk"), Index);
			return false;
		}
		V.Data = G.Bin + Start;
		return true;
	}

	FORCEINLINE float F(const FView& V, int64 I, int32 C)
	{
		float X;
		FMemory::Memcpy(&X, V.Data + I * V.Stride + C * 4, 4);
		return X;
	}

	FORCEINLINE uint32 Index(const FView& V, int64 I)
	{
		const uint8* P = V.Data + I * V.Stride;
		switch (V.CompType)
		{
		case 5121: return *P;
		case 5123: { uint16 X; FMemory::Memcpy(&X, P, 2); return X; }
		default: { uint32 X; FMemory::Memcpy(&X, P, 4); return X; }
		}
	}

	/** 4x4, column vectors (glTF convention), row-major storage M[r][c]. */
	struct FMat
	{
		double M[4][4] = {{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}, {0, 0, 0, 1}};

		FMat operator*(const FMat& B) const
		{
			FMat R;
			for (int r = 0; r < 4; ++r)
			{
				for (int c = 0; c < 4; ++c)
				{
					R.M[r][c] = M[r][0] * B.M[0][c] + M[r][1] * B.M[1][c] + M[r][2] * B.M[2][c] + M[r][3] * B.M[3][c];
				}
			}
			return R;
		}
	};

	FMat LocalMatrix(const TSharedPtr<FJsonObject>& Node)
	{
		FMat Out;
		const TArray<TSharedPtr<FJsonValue>>& Mx = Arr(Node, TEXT("matrix"));
		if (Mx.Num() == 16)
		{
			for (int c = 0; c < 4; ++c)
			{
				for (int r = 0; r < 4; ++r)
				{
					Out.M[r][c] = Mx[c * 4 + r]->AsNumber();  // column-major in glTF
				}
			}
			return Out;
		}
		const TArray<TSharedPtr<FJsonValue>>& T = Arr(Node, TEXT("translation"));
		const TArray<TSharedPtr<FJsonValue>>& Q = Arr(Node, TEXT("rotation"));
		const TArray<TSharedPtr<FJsonValue>>& S = Arr(Node, TEXT("scale"));
		const double x = Q.Num() == 4 ? Q[0]->AsNumber() : 0, y = Q.Num() == 4 ? Q[1]->AsNumber() : 0;
		const double z = Q.Num() == 4 ? Q[2]->AsNumber() : 0, w = Q.Num() == 4 ? Q[3]->AsNumber() : 1;
		const double Rm[3][3] = {
			{1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)},
			{2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)},
			{2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)}};
		const double Sc[3] = {S.Num() == 3 ? S[0]->AsNumber() : 1, S.Num() == 3 ? S[1]->AsNumber() : 1, S.Num() == 3 ? S[2]->AsNumber() : 1};
		for (int r = 0; r < 3; ++r)
		{
			for (int c = 0; c < 3; ++c)
			{
				Out.M[r][c] = Rm[r][c] * Sc[c];
			}
			Out.M[r][3] = T.Num() == 3 ? T[r]->AsNumber() : 0.0;
		}
		return Out;
	}

	/** T_ue = C T_g C^-1 with C = swap(y, z), translation in cm; returned as an FTransform. */
	FTransform ToUnrealTransform(const FMat& G)
	{
		static const int Swap[3] = {0, 2, 1};
		FMatrix M = FMatrix::Identity;
		for (int i = 0; i < 3; ++i)
		{
			for (int j = 0; j < 3; ++j)
			{
				// column-vector A_ue[r][c] = A_g[Swap[r]][Swap[c]]; Unreal rows are the images of the basis axes
				M.M[j][i] = G.M[Swap[i]][Swap[j]];
			}
			M.M[3][i] = G.M[Swap[i]][3] * 100.0;
		}
		return FTransform(M);
	}

	void BuildMips(TwinGltf::FImage& Img)
	{
		while (Img.Width > MaxTextureSize || Img.Height > MaxTextureSize)
		{
			// halve until it fits (box filter), replacing mip 0
			const int32 W = FMath::Max(1, Img.Width / 2), H = FMath::Max(1, Img.Height / 2);
			TArray64<uint8> Next;
			Next.SetNumUninitialized((int64)W * H * 4);
			const TArray64<uint8>& Src = Img.Mips[0];
			for (int32 y = 0; y < H; ++y)
			{
				for (int32 x = 0; x < W; ++x)
				{
					for (int32 c = 0; c < 4; ++c)
					{
						const int32 x0 = FMath::Min(x * 2, Img.Width - 1), x1 = FMath::Min(x * 2 + 1, Img.Width - 1);
						const int32 y0 = FMath::Min(y * 2, Img.Height - 1), y1 = FMath::Min(y * 2 + 1, Img.Height - 1);
						const int32 Sum = Src[((int64)y0 * Img.Width + x0) * 4 + c] + Src[((int64)y0 * Img.Width + x1) * 4 + c] +
							Src[((int64)y1 * Img.Width + x0) * 4 + c] + Src[((int64)y1 * Img.Width + x1) * 4 + c];
						Next[((int64)y * W + x) * 4 + c] = (uint8)((Sum + 2) / 4);
					}
				}
			}
			Img.Mips[0] = MoveTemp(Next);
			Img.Width = W;
			Img.Height = H;
		}
		int32 W = Img.Width, H = Img.Height;
		while (W > 1 || H > 1)
		{
			const int32 NW = FMath::Max(1, W / 2), NH = FMath::Max(1, H / 2);
			const TArray64<uint8>& Src = Img.Mips.Last();
			TArray64<uint8> Next;
			Next.SetNumUninitialized((int64)NW * NH * 4);
			for (int32 y = 0; y < NH; ++y)
			{
				for (int32 x = 0; x < NW; ++x)
				{
					const int32 x0 = FMath::Min(x * 2, W - 1), x1 = FMath::Min(x * 2 + 1, W - 1);
					const int32 y0 = FMath::Min(y * 2, H - 1), y1 = FMath::Min(y * 2 + 1, H - 1);
					for (int32 c = 0; c < 4; ++c)
					{
						const int32 Sum = Src[((int64)y0 * W + x0) * 4 + c] + Src[((int64)y0 * W + x1) * 4 + c] +
							Src[((int64)y1 * W + x0) * 4 + c] + Src[((int64)y1 * W + x1) * 4 + c];
						Next[((int64)y * NW + x) * 4 + c] = (uint8)((Sum + 2) / 4);
					}
				}
			}
			Img.Mips.Add(MoveTemp(Next));
			W = NW;
			H = NH;
		}
	}
}

namespace TwinGltf
{
	bool Parse(const TArray<uint8>& Data, IImageWrapperModule* ImageWrappers, FModel& Out, FString& Err)
	{
		FGlb G;
		if (!ReadGlb(Data, G, Err))
		{
			return false;
		}
		const TSharedPtr<FJsonObject> Asset = G.Json->GetObjectField(TEXT("asset"));
		Out.Generator = Asset.IsValid() ? Asset->GetStringField(TEXT("generator")) : FString();
		if (Asset.IsValid() && Asset->GetStringField(TEXT("version")) != TEXT("2.0"))
		{
			Err = TEXT("only glTF 2.0 is supported");
			return false;
		}
		const TArray<TSharedPtr<FJsonValue>>& Required = Arr(G.Json, TEXT("extensionsRequired"));
		if (Required.Num() > 0)
		{
			Err = FString::Printf(TEXT("the model requires glTF extension %s (not supported by the viewer)"), *Required[0]->AsString());
			return false;
		}

		// ---- images (decoded + mipmapped here, uploaded on the game thread) ----------------------------
		const TArray<TSharedPtr<FJsonValue>>& Images = Arr(G.Json, TEXT("images"));
		for (int32 i = 0; i < Images.Num(); ++i)
		{
			const TSharedPtr<FJsonObject> I = Images[i]->AsObject();
			FImage& Img = Out.Images.AddDefaulted_GetRef();
			Img.Name = I->GetStringField(TEXT("name"));
			const TSharedPtr<FJsonObject> BV = ObjAt(G.Json, TEXT("bufferViews"), Int(I, TEXT("bufferView")));
			if (!BV.IsValid() || !ImageWrappers || !G.Bin)
			{
				continue;  // external or missing image: the material falls back to its colour
			}
			const int64 Off = (int64)Num(BV, TEXT("byteOffset"), 0), Len = (int64)Num(BV, TEXT("byteLength"), 0);
			if (Off < 0 || Len <= 0 || Off + Len > G.BinSize)
			{
				continue;
			}
			const EImageFormat Format = ImageWrappers->DetectImageFormat(G.Bin + Off, Len);
			const TSharedPtr<IImageWrapper> W = Format == EImageFormat::Invalid ? nullptr : ImageWrappers->CreateImageWrapper(Format);
			TArray64<uint8> Raw;
			if (W.IsValid() && W->SetCompressed(G.Bin + Off, Len) && W->GetRaw(ERGBFormat::BGRA, 8, Raw))
			{
				Img.Width = (int32)W->GetWidth();
				Img.Height = (int32)W->GetHeight();
				Img.Mips.Add(MoveTemp(Raw));
				BuildMips(Img);
			}
		}
		const TArray<TSharedPtr<FJsonValue>>& Textures = Arr(G.Json, TEXT("textures"));

		// ---- materials -----------------------------------------------------------------------------
		for (const TSharedPtr<FJsonValue>& V : Arr(G.Json, TEXT("materials")))
		{
			const TSharedPtr<FJsonObject> M = V->AsObject();
			FMaterial& Mat = Out.Materials.AddDefaulted_GetRef();
			Mat.Name = M->GetStringField(TEXT("name"));
			const TSharedPtr<FJsonObject>* Pbr = nullptr;
			if (M->TryGetObjectField(TEXT("pbrMetallicRoughness"), Pbr))
			{
				const TArray<TSharedPtr<FJsonValue>>& C = Arr(*Pbr, TEXT("baseColorFactor"));
				if (C.Num() == 4)
				{
					Mat.BaseColor = FLinearColor(C[0]->AsNumber(), C[1]->AsNumber(), C[2]->AsNumber(), C[3]->AsNumber());
				}
				Mat.Metallic = (float)Num(*Pbr, TEXT("metallicFactor"), 1.0);
				Mat.Roughness = (float)Num(*Pbr, TEXT("roughnessFactor"), 1.0);
				const TSharedPtr<FJsonObject>* Tex = nullptr;
				if ((*Pbr)->TryGetObjectField(TEXT("baseColorTexture"), Tex))
				{
					const int32 TexIndex = Int(*Tex, TEXT("index"));
					if (Textures.IsValidIndex(TexIndex))
					{
						const int32 Src = Int(Textures[TexIndex]->AsObject(), TEXT("source"));
						if (Out.Images.IsValidIndex(Src) && Out.Images[Src].Mips.Num() > 0)
						{
							Mat.BaseColorImage = Src;
						}
					}
				}
			}
			const TArray<TSharedPtr<FJsonValue>>& E = Arr(M, TEXT("emissiveFactor"));
			if (E.Num() == 3)
			{
				Mat.Emissive = FLinearColor(E[0]->AsNumber(), E[1]->AsNumber(), E[2]->AsNumber());
			}
			FString AlphaMode;
			Mat.bBlend = M->TryGetStringField(TEXT("alphaMode"), AlphaMode) && AlphaMode == TEXT("BLEND");
			M->TryGetBoolField(TEXT("doubleSided"), Mat.bDoubleSided);
		}

		// ---- meshes (converted to Unreal space once; instances share them) ---------------------------
		for (const TSharedPtr<FJsonValue>& MV : Arr(G.Json, TEXT("meshes")))
		{
			FMesh& Mesh = Out.Meshes.AddDefaulted_GetRef();
			for (const TSharedPtr<FJsonValue>& PV : Arr(MV->AsObject(), TEXT("primitives")))
			{
				const TSharedPtr<FJsonObject> P = PV->AsObject();
				if (Int(P, TEXT("mode"), 4) != 4)
				{
					Err = TEXT("the model has non-triangle primitives (points/lines are not supported)");
					return false;
				}
				const TSharedPtr<FJsonObject> Attr = P->GetObjectField(TEXT("attributes"));
				FView Pos, Nrm, Uv, Idx;
				if (!Accessor(G, Int(Attr, TEXT("POSITION")), Pos, Err))
				{
					return false;
				}
				if (Pos.CompType != 5126 || Pos.Comps != 3)
				{
					Err = TEXT("vertex positions must be float VEC3");
					return false;
				}
				const bool bNormals = Attr->HasField(TEXT("NORMAL"));
				const bool bUvs = Attr->HasField(TEXT("TEXCOORD_0"));
				if ((bNormals && (!Accessor(G, Int(Attr, TEXT("NORMAL")), Nrm, Err) || Nrm.CompType != 5126 || Nrm.Count != Pos.Count)) ||
					(bUvs && (!Accessor(G, Int(Attr, TEXT("TEXCOORD_0")), Uv, Err) || Uv.CompType != 5126 || Uv.Count != Pos.Count)))
				{
					Err = Err.IsEmpty() ? TEXT("normals and texture coordinates must be float and match the vertex count") : Err;
					return false;
				}
				FProcMeshSection& S = Mesh.Sections.AddDefaulted_GetRef();
				S.ProcVertexBuffer.SetNum((int32)Pos.Count);
				for (int32 i = 0; i < (int32)Pos.Count; ++i)
				{
					FProcMeshVertex& Vx = S.ProcVertexBuffer[i];
					Vx.Position = ToUnrealPoint(F(Pos, i, 0), F(Pos, i, 1), F(Pos, i, 2));
					Vx.Normal = bNormals ? ToUnrealDir(F(Nrm, i, 0), F(Nrm, i, 1), F(Nrm, i, 2)) : FVector::UpVector;
					Vx.UV0 = bUvs ? FVector2D(F(Uv, i, 0), F(Uv, i, 1)) : FVector2D::ZeroVector;
					S.SectionLocalBox += Vx.Position;
				}
				if (P->HasField(TEXT("indices")))
				{
					if (!Accessor(G, Int(P, TEXT("indices")), Idx, Err) || Idx.Comps != 1)
					{
						Err = Err.IsEmpty() ? TEXT("bad index accessor") : Err;
						return false;
					}
					S.ProcIndexBuffer.SetNum((int32)(Idx.Count - Idx.Count % 3));
					for (int32 i = 0; i < S.ProcIndexBuffer.Num(); ++i)
					{
						const uint32 Ix = Index(Idx, i);
						if (Ix >= (uint32)Pos.Count)
						{
							Err = TEXT("a triangle index points past the vertex buffer");
							return false;
						}
						S.ProcIndexBuffer[i] = Ix;
					}
				}
				else
				{
					S.ProcIndexBuffer.SetNum((int32)(Pos.Count - Pos.Count % 3));
					for (int32 i = 0; i < S.ProcIndexBuffer.Num(); ++i)
					{
						S.ProcIndexBuffer[i] = (uint32)i;
					}
				}
				S.bSectionVisible = true;
				Mesh.Materials.Add(Int(P, TEXT("material")));
				Mesh.LocalBox += S.SectionLocalBox;
				Mesh.Triangles += S.ProcIndexBuffer.Num() / 3;
			}
		}

		// ---- nodes (world transforms, scene roots first) ------------------------------------------------
		const TArray<TSharedPtr<FJsonValue>>& Nodes = Arr(G.Json, TEXT("nodes"));
		const TSharedPtr<FJsonObject> Scene = ObjAt(G.Json, TEXT("scenes"), Int(G.Json, TEXT("scene"), 0));
		TArray<TPair<int32, FMat>> Stack;
		for (const TSharedPtr<FJsonValue>& R : Arr(Scene, TEXT("nodes")))
		{
			Stack.Emplace((int32)R->AsNumber(), FMat());
		}
		TSet<int32> Seen;
		while (Stack.Num())
		{
			const TPair<int32, FMat> Item = Stack.Pop();
			if (!Nodes.IsValidIndex(Item.Key) || Seen.Contains(Item.Key))
			{
				Err = TEXT("the node hierarchy is invalid (bad index or cycle)");
				return false;
			}
			Seen.Add(Item.Key);
			const TSharedPtr<FJsonObject> N = Nodes[Item.Key]->AsObject();
			const FMat World = Item.Value * LocalMatrix(N);
			const int32 MeshIndex = Int(N, TEXT("mesh"));
			if (Out.Meshes.IsValidIndex(MeshIndex))
			{
				FNode& Node = Out.Nodes.AddDefaulted_GetRef();
				Node.Name = N->GetStringField(TEXT("name"));
				Node.Mesh = MeshIndex;
				Node.Transform = ToUnrealTransform(World);
				const TSharedPtr<FJsonObject>* Extras = nullptr;
				if (N->TryGetObjectField(TEXT("extras"), Extras))
				{
					Node.Extras = *Extras;
				}
				Out.Triangles += Out.Meshes[MeshIndex].Triangles;
			}
			for (const TSharedPtr<FJsonValue>& C : Arr(N, TEXT("children")))
			{
				Stack.Emplace((int32)C->AsNumber(), World);
			}
		}
		if (Out.Nodes.Num() == 0)
		{
			Err = TEXT("the model contains no meshes");
			return false;
		}
		return true;
	}
}
